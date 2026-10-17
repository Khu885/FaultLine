# FaultLine

FaultLine is an auditable Python repair pipeline. It collects real pytest
failures, localizes application source, asks an explicitly selected repair
adapter for a structured unified diff, validates that diff, applies it only to
a disposable copy, and accepts a candidate only after the targeted test passes
and protected test-file hashes remain unchanged, and the complete candidate suite
introduces no new regression. Optional AST mutation testing then measures the
relevant tests' sensitivity to controlled changes in the repaired function.

FaultLine does **not** modify the original project during candidate evaluation.
Mutation testing is advisory only: it never overrides the mandatory acceptance
gates, patches tests, or claims that a high mutation score proves correctness.

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
    │
    ▼  (opt-in after ACCEPTED only)
Target-scoped AST mutation generator + isolated mutant runner
    │
    ▼
MutationReport with killed/survived/inconclusive evidence
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
  --mutation-test        run advisory AST mutation testing after acceptance
  --max-mutants N        maximum generated mutants (default 5)
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
8. Capture a complete baseline pytest report from the unmodified project before
   candidate verification.
9. Run the complete pytest suite in the disposable candidate copy and compare
   stable node outcomes with the baseline.
10. Accept a patch only when validation, the targeted test, test integrity, and
   global regression gates all pass. Pre-existing unresolved failures remain
   visible but do not count as newly introduced regressions.
11. Retry at most once by default, always from a fresh copy and with structured
   feedback containing the previous diff and test diagnostics.
12. Return `ACCEPTED` only after the complete evidence set passes. A targeted
   pass before global verification is recorded as the intermediate
   `PROVISIONALLY_SUCCESSFUL` state, never as final acceptance.
13. When `--mutation-test` is enabled, generate and execute up to
   `--max-mutants` deterministic AST mutants only after the mandatory gates
   return `ACCEPTED`. Mutation evidence is attached to `RepairReport`, but it
   is advisory and cannot change that acceptance verdict.

The state history is recorded per attempt (`PENDING`, `ANALYZING`,
`PATCH_GENERATED`, `VALIDATING`, `TESTING`, `RETRYING`,
`PROVISIONALLY_SUCCESSFUL`, `ACCEPTED`, `FAILED`, `REJECTED`, or
`INCONCLUSIVE`). Missing, timed-out, collection-error, or incomplete global
verification is `INCONCLUSIVE`; a confirmed newly failing test is `REJECTED`.

## JSON report format

`RepairReport` serializes the execution mode, final state, all attempts,
candidate proposals, validation results, targeted `TestExecutionResult`,
integrity results, complete baseline/candidate `VerificationReport` evidence,
the arbiter decision, accepted diff, root-cause explanation, timing, and
explicit limitations. A successful report has all of these gates:

```json
{
  "final_status": "ACCEPTED",
  "targeted_test_passed": true,
  "test_integrity_passed": true,
  "verification_report": {
    "regression_passed": true,
    "newly_failing": [],
    "missing": [],
    "collection_errors": []
  },
  "acceptance_decision": {
    "status": "ACCEPTED",
    "accepted": true,
    "evidence_complete": true
  }
}
```

`PROVISIONALLY_SUCCESSFUL` is retained as an intermediate lifecycle state for
backward-compatible reports and orchestration callers. It is not a final
acceptance result. FaultLine still verifies in disposable local copies rather
than applying changes to the original project or deploying to production.

When mutation testing is enabled, the JSON report additionally contains a
`MutationReport` with deterministic mutation ids, operator, target location,
pytest evidence, `KILLED`/`SURVIVED`/`INCONCLUSIVE` outcomes, and
`kill_rate = killed / (killed + survived) * 100`. Inconclusive mutants are
excluded from the denominator. A zero denominator produces an unavailable
score rather than `100%`.

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
- Mutation subprocesses receive the same filtered environment and bounded
  process-group timeout behavior as the targeted runner. Each mutant is
  evaluated in a fresh disposable copy of the verified candidate; the
  candidate and original project remain unchanged. A temporary directory is
  not a hardened security sandbox.
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
copies, CLI JSON output, the final acceptance arbiter, global regression
classification, accepted pagination repair, targeted-pass/new-regression
rejection, inconclusive verification, AST mutation operators, deterministic
mutation ids, isolated mutant execution, baseline-relative classification,
kill-rate reporting, optional worker/CLI integration, and all Day 1 behavior.

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

The offline acceptance demonstrations are reproducible without credentials:

```sh
uv run --locked pytest tests/test_day4_acceptance.py -q
```

This proves that the pagination proposal reaches `ACCEPTED` only after the
global regression check, while a proposal that makes the targeted test pass
but breaks a previously passing test reaches `REJECTED`. A timed-out baseline
is reported as `INCONCLUSIVE` rather than accepted.

The Phase 3 mutation demonstration uses the same offline pagination proposal
with the advisory stage enabled:

```sh
uv run --locked pytest tests/test_mutation.py -q
```

The test records the actual generated and independently executed mutants,
including a surviving-mutant fixture and a timeout-based inconclusive fixture.
The original files under `benchmarks/` are copied only into temporary test
projects and remain unchanged.

## Docker

```sh
docker build -t faultline .
docker run --rm faultline make test
```

The current Docker image is suitable for the deterministic project tests. It
does not turn candidate execution into a hardened sandbox; the limitation
above still applies.

## Future milestones

A separately audited OS/container execution boundary and a supported automated
OmniRush invocation remain future work if the platform publishes one. Mutation
testing currently remains local, bounded, and advisory; it does not provide a
hardened sandbox or replace the existing single-worker evidence model.

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
