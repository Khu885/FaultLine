# FaultLine submission summary

## Scope

FaultLine is an auditable Python repair pipeline for pytest failures. The
current checkout combines deterministic failure collection and localization,
human-mediated repair handoff, constrained patch application, disposable-copy
verification, global regression classification, bounded Day 3 orchestration,
and optional advisory AST mutation evidence.

The repository is on `feature/faultline-day3`, but this summary describes the
behavior that is actually checked in rather than inferring behavior from the
branch name.

## Judge-facing behavior

### Detection

`faultline scan <project>` runs the repository's pytest collector/parser,
reports failures and collection errors separately, and emits `FailureRecord`,
`SourceLocation`, `WorkUnit`, and `ScanReport` data. The benchmark fixtures
under `benchmarks/` are intentionally broken examples for demonstration:

- off-by-one pagination;
- mutable default argument state leakage; and
- unsafe nested dictionary access.

They are not included by `make test`; the benchmark command is expected to
fail until those fixtures are repaired.

### Human-mediated OmniRush workflow

The supported OmniRush path is an explicit file handoff:

```sh
uv run --locked faultline repair-export . \
  --work-unit <work-unit-id> \
  --output repair-request.json

# A person gives repair-request.json to an actual OmniRush session and saves
# the returned structured proposal as repair-proposal.json.

uv run --locked faultline repair-import . \
  --proposal repair-proposal.json \
  --work-unit <work-unit-id> \
  --json-output repair-report.json
```

The adapter is named `omnirush_human_mediated`. Without a proposal input it
exports the request and reports that execution is human-mediated; it does not
silently substitute a fixture and does not make an autonomous agent call.
The proposal is imported as a `PatchProposal` and sent through the same
validation and verification worker as an offline fixture.

When no live session is available, the repository uses an explicit
`FixtureAgentAdapter` and reports `offline_fixture`. That mode demonstrates
the deterministic worker, not a live AI repair.

### Accepted pagination repair

The checked-in pagination benchmark expects page 1 of size 2 to return both
`"c"` and `"d"`. Its broken implementation stops at
`start + page_size - 1`; the accepted test proposal changes the endpoint to
`start + page_size`.

Acceptance is fail-closed and has these gates:

1. structured proposal and constrained unified-diff validation;
2. exact-context application in a fresh disposable copy;
3. Python syntax parsing for changed files;
4. the targeted failing node passes;
5. protected test-file inventory and SHA-256 hashes are unchanged;
6. complete baseline and candidate pytest reports have no newly failing or
   otherwise inconclusive result; and
7. the acceptance arbiter returns `ACCEPTED`.

The repository test
`tests/test_day4_acceptance.py::test_worker_accepts_real_pagination_repair_after_global_regression_check`
asserts this behavior. The related mutation integration test also asserts that
the original benchmark source is unchanged after candidate and mutant
evaluation.

### Unsafe rejection

The controlled patcher accepts only an existing authorized target file and
exact matching text hunks. It rejects:

- empty, malformed, binary, or unsupported diffs;
- absolute or traversal paths;
- files outside the authorized target;
- test files and protected project configuration;
- file creation, deletion, and renames;
- symbolic-link destinations;
- obvious test-suppression mechanisms; and
- hunks whose context or deleted text does not match.

The guardrail assertions are in `tests/test_repair_primitives.py`. A runtime
attempt to modify a protected test file is caught by the SHA-256 integrity
check in `tests/test_repair_worker.py`. The exact terminal status depends on
where the unsafe behavior is detected (`REJECTED` for validation rejection,
or a failed attempt for runtime integrity violation), but it is never
accepted.

### Inconclusive verification

FaultLine distinguishes unavailable evidence from a confirmed regression.
Missing, timed-out, malformed, incomplete, or collection-error baseline or
candidate evidence becomes `INCONCLUSIVE`; it cannot satisfy the acceptance
arbiter. A test that was previously passing and becomes failing is a confirmed
new regression and produces `REJECTED`. A pre-existing failure can remain
visible as unresolved without being misclassified as a newly introduced
regression.

The timeout classification is asserted by
`tests/test_day4_acceptance.py::test_worker_reports_inconclusive_when_baseline_times_out`.
The report models and comparison cases are covered by `tests/test_regression.py`.

### Mutation testing

Mutation testing is opt-in for single-WorkUnit `repair` and `repair-import`:

```sh
uv run --locked faultline repair . \
  --backend fixture \
  --proposal-input fixture-proposal.json \
  --mutation-test \
  --max-mutants 5 \
  --json-output repair-report.json
```

It runs only after mandatory patch acceptance, uses deterministic target-scoped
AST mutations and fresh copies, and records `KILLED`, `SURVIVED`, or
`INCONCLUSIVE` per mutant. Inconclusive mutants are excluded from the
kill-rate denominator, and a zero conclusive denominator produces an
unavailable score. Surviving mutants are reported as a quality warning; they
do not overturn an otherwise accepted patch. Mutation testing does not replace
global regression verification and is not a correctness proof.

The complete mutation evidence is in `tests/test_mutation.py`, including
operator generation, deterministic ids, isolated execution, killed and
surviving examples, timeout/invalid-source inconclusive cases, advisory
acceptance behavior, and pagination integration.

## Current Day 3 orchestration

The batch command wraps the existing `RepairWorker` with bounded parallel
execution:

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

Current behavior is:

- input may be a WorkUnit list or a scan report containing `work_units`;
- at most `--max-workers` workers are active;
- results preserve input order;
- worker exceptions are isolated into per-WorkUnit outcomes;
- active cancellation and worker timeouts are represented in the aggregate
  report; and
- worker lifecycle state is in memory, while each repair attempt still uses a
  disposable project copy.

The command's explicit fixture directory is offline test input. The batch
path does not claim live OmniRush calls, and it does not expose the optional
single-WorkUnit mutation stage. The behavior is covered by
`tests/test_day3_orchestration.py` and the CLI tests.

## Reproduction commands and observed repository evidence

Setup and canonical offline check:

```sh
uv sync --locked
make test
make demo
```

Observed in this checkout:

| Command | Observed result |
| --- | --- |
| `make test` | `90 passed`, with two dependency deprecation warnings |
| `uv run --locked pytest tests/test_day3_orchestration.py -q` | `7 passed` |
| `uv run --locked pytest tests/test_day4_acceptance.py -q` | `3 passed` |
| `uv run --locked pytest tests/test_mutation.py -q` | `31 passed`, with two dependency deprecation warnings |
| `uv run --locked pytest tests/test_repair_primitives.py -q` | `8 passed` |
| `uv run --locked pytest tests/test_regression.py -q` | `11 passed` |
| `uv run --locked pytest benchmarks -q` | `3 failed`, the three intentional broken benchmark fixtures |
| `make demo` | 3 failures detected; offline pagination repair `ACCEPTED`; regression patch `REJECTED`; timeout case `INCONCLUSIVE`; current run reported 5 mutants, 4 killed, 1 survived, 0 inconclusive, 80.00% kill rate |

These are local repository test results. They are not claims about live agent
activity, external users, production deployment, or benchmark performance.

## Limitations and remaining manual steps

1. A person must select the WorkUnit id from the scan, review the exported
   request, use an actual OmniRush session if desired, save its proposal JSON,
   and invoke `repair-import`.
2. `<work-unit-id>`, `project/path`, `fixture-proposals/`, and the proposal
   filenames in the commands are placeholders for files/identifiers supplied
   by the demo operator.
3. The local runner uses disposable temporary directories, not a hardened
   security sandbox. Network isolation, CPU/memory quotas, process limits, and
   non-root execution are not enforced.
4. Batch lifecycle state is not durable across restart, and Python-thread
   cancellation depends on cooperative checks.
5. Mutation evidence is bounded, local, target-scoped, advisory, and
   subordinate to mandatory acceptance gates.
6. No deployment or production-application step is implemented by these
   commands. The documented Docker command runs the deterministic project test
   command; a Docker build/run result is not included in the observed evidence
   table above.
7. `make demo` is the automated offline path; it labels its proposal
   `offline_fixture` and writes reports under `demo-output/`. A genuine
   OmniRush-assisted repair still requires the human export/import handoff
   shown above.

## Claims intentionally left conditional

- A live OmniRush-assisted repair is conditional on a human completing the
  request/proposal handoff and the imported proposal passing all gates.
- The exact WorkUnit identifier and proposal/report paths are conditional on
  the operator's scan and chosen output locations.
- No mutation score, autonomous-agent activity, external-user outcome,
  deployment result, or benchmark performance statistic is claimed here.
