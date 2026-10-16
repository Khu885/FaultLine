# FaultLine

FaultLine is an auditable Python repair pipeline. It collects real pytest
failures, localizes application source, asks an explicitly selected repair
adapter for a structured unified diff, validates that diff, applies it only to
a disposable copy, and accepts a candidate only after the targeted test passes
and protected test-file hashes remain unchanged.

FaultLine does **not** modify the original project during candidate evaluation.
It does not implement parallel workers, mutation testing, patching of tests,
or a dashboard.

## Architecture

```text
Python project
    │
    ▼
Pytest collector + JSON report parser
    │
    ▼
AST localizer + narrow import inference for assertion-only tracebacks
    │
    ▼
Conservative WorkUnit planner
    │
    ▼
RepairAgent adapter
    │  fixture (offline) | OmniRush human-mediated export/import
    ▼
Patch protocol + path/config/test guardrails
    │
    ▼
Fresh disposable working copy per attempt
    │
    ▼
Targeted pytest runner + SHA-256 integrity guard
    │
    ▼
Typed RepairReport and JSON evidence
```

## Installation

Prerequisites:

- Python **3.12.15** (recorded in `.python-version`; the package supports
  Python 3.11+)
- [uv](https://docs.astral.sh/uv/) 0.13.0 or newer
- GNU Make for the canonical test command

From the repository root:

```sh
uv sync --locked
```

Dependencies are pinned in `pyproject.toml` and `uv.lock`. No credentials or
network service is required for the offline test suite.

## Deterministic scan

The Day 1 scan remains available:

```sh
uv run --locked faultline scan path/to/project
uv run --locked faultline scan path/to/project --json-output scan.json
```

The scan completes successfully when target tests fail. Its JSON contains
`FailureRecord`, `SourceLocation`, `WorkUnit`, and `ScanReport` data, including
collection errors separately from ordinary test failures.

## Repair workflow

### Human-mediated OmniRush mode

The available OmniRush desktop/MCP documentation does not expose a supported
synchronous local SDK or CLI call for requesting an agent-generated patch.
FaultLine therefore labels this mode accurately as
`omnirush_human_mediated`:

1. Detect failures and export a constrained request:

   ```sh
   uv run --locked faultline repair-export . \
     --work-unit <work-unit-id> \
     --output repair-request.json
   ```

2. Give `repair-request.json` to an actual OmniRush session. The request
   includes the WorkUnit id, relative authorized source paths, source snippet,
   failing node id, traceback, error context, and repair constraints.
3. Have the session return a JSON `PatchProposal` with a root-cause
   explanation and unified diff. The session must not edit tests or claim the
   patch works without running it.
4. Import and verify the proposal:

   ```sh
   uv run --locked faultline repair-import . \
     --proposal repair-proposal.json \
     --work-unit <work-unit-id> \
     --json-output repair-report.json
   ```

The combined command is also available when a proposal file already exists:

```sh
uv run --locked faultline repair . \
  --backend omnirush \
  --proposal-input repair-proposal.json \
  --request-output repair-request.json \
  --work-unit <work-unit-id> \
  --max-attempts 2 \
  --timeout 10 \
  --json-output repair-report.json \
  --verbose
```

Without `--proposal-input`, the OmniRush adapter exports the request (when
`--request-output` is supplied) and reports that execution is human-mediated;
it never silently substitutes a fixture response.

### Offline fixture mode

Offline unit tests use an explicit `FixtureAgentAdapter` with supplied
proposals. A proposal can be verified from the CLI by selecting
`--backend fixture` and passing `--proposal-input`:

```sh
uv run --locked faultline repair . \
  --backend fixture \
  --proposal-input fixture-proposal.json \
  --max-attempts 2
```

This mode is labeled `offline_fixture`; it is not evidence of a live AI
repair. The repository contains no hard-coded benchmark repair logic in the
OmniRush adapter.

The repair command supports:

```text
faultline repair <project-path>
  --max-attempts N       bounded total candidate generations (default 2)
  --backend NAME         omnirush, fixture, or unavailable
  --work-unit ID|INDEX   select a detected WorkUnit
  --json-output PATH     write RepairReport JSON
  --timeout SECONDS      targeted test timeout (default 10)
  --verbose              print retry feedback
  --request-output PATH  human-mediated request handoff file
  --proposal-input PATH  structured proposal import file
```

## Candidate validation and verification

Every attempt follows this sequence:

1. Build a typed `RepairRequest`.
2. Generate one typed `PatchProposal`.
3. Reject empty, malformed, binary, traversal, absolute, symlink, creation,
   deletion, rename, unauthorized, test-file, protected-config, and obvious
   test-suppression diffs.
4. Apply only exact-context unified diffs to a fresh temporary copy.
5. Parse every changed Python file with `ast.parse`.
6. Run only the failing pytest node with `sys.executable -m pytest`, capturing
   stdout, stderr, exit code, duration, and timeout state.
7. Compare protected test-file inventories and SHA-256 hashes.
8. Retry at most once by default, always from a fresh copy and with structured
   feedback containing the previous diff and test diagnostics.
9. Return `PROVISIONALLY_SUCCESSFUL` only when the targeted test passes and
   test integrity passes.

The state history is recorded per attempt (`PENDING`, `ANALYZING`,
`PATCH_GENERATED`, `VALIDATING`, `TESTING`, `RETRYING`,
`PROVISIONALLY_SUCCESSFUL`, `FAILED`, or `REJECTED`).

## JSON report format

`RepairReport` serializes the execution mode, final state, all attempts,
candidate proposals, validation results, targeted `TestExecutionResult`,
integrity results, accepted diff, root-cause explanation, timing, and explicit
limitations. A successful report has both:

```json
{
  "final_status": "PROVISIONALLY_SUCCESSFUL",
  "targeted_test_passed": true,
  "test_integrity_passed": true
}
```

The word “provisionally” is intentional: FaultLine verifies the targeted test
in an isolated copy, not the entire repository or a production deployment.

## Security boundary and limitations

- Candidate changes are applied only in disposable temporary copies.
- Patch application uses a controlled parser; no unrestricted shell command or
  system patch executable is invoked.
- Test paths are blocked both at patch-validation time and by post-execution
  SHA-256 inventory checks.
- Ordinary temporary directories are **not** a security sandbox. The local
  runner currently does not enforce network isolation, CPU/memory quotas,
  process limits, or a non-root user. Run only explicitly trusted local
  projects and fixtures. Container/OS-level isolation is a later milestone.
- The adapter does not expose credentials or invent OmniRush APIs. Automated
  OmniRush execution is not claimed because no supported programmatic repair
  interface was available during this implementation.

## Tests

`make test` is the canonical offline command:

```sh
make test
```

The focused suite covers adapters, proposal validation, path and symlink
guards, syntax errors, test integrity, runner timeouts, worker retries, fresh
copies, CLI JSON output, and all Day 1 behavior.

## Benchmark demonstration

The repository keeps three intentionally broken scenarios:

- off-by-one pagination
- mutable default argument
- unsafe nested dictionary access

They remain excluded from `make test` and should fail reproducibly:

```sh
uv run --locked pytest benchmarks -q
```

To inspect all WorkUnits:

```sh
uv run --locked faultline scan . --json-output scan.json
```

For a real OmniRush-assisted demonstration, export the off-by-one WorkUnit,
have an actual OmniRush session return a proposal, then run `repair-import`.
If no live session is available, use an explicit fixture proposal and report
the result as `offline_fixture`, not as an OmniRush repair.

## Docker

```sh
docker build -t faultline .
docker run --rm faultline make test
```

The current Docker image is suitable for the deterministic project tests. It
does not turn candidate execution into a hardened sandbox; the limitation
above still applies.

## Next milestones

Before Day 3, add a separately audited OS/container execution boundary and a
single supported automated OmniRush invocation if the platform publishes one.
Day 3 can then add parallel workers, orchestration, durable job state, and
cross-WorkUnit aggregation without weakening the single-worker evidence model.

## Day 3 parallel repair orchestration

Day 3 adds a bounded `RepairOrchestrator` around the existing single-worker
pipeline. It preserves input order, isolates worker exceptions, limits active
workers, and records per-WorkUnit outcomes in an aggregate JSON report:

```sh
uv run --locked faultline repair-batch project/path \
  --input workunits.json \
  --backend fixture \
  --proposal-input fixture-proposals/ \
  --max-workers 3 \
  --max-attempts 2 \
  --timeout 10 \
  --json-output aggregate-repair.json
```

The input may be a WorkUnit list or a scan report containing `work_units`.
Fixture proposal directories are explicit offline test inputs; they are never
reported as live OmniRush execution. `omnirush` remains human-mediated through
the existing request-export/proposal-import workflow. Worker lifecycle state is
in-memory and temporary copies are not hardened OS sandboxes.
