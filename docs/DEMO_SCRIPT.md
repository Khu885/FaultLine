# FaultLine judge demo script

This is a 3–5 minute walkthrough for the current checkout. It describes the
commands and behavior that are present in the repository. It does not claim
that an OmniRush agent was called automatically.

## 0:00–0:30 — Frame the demo

FaultLine is a deterministic pytest failure-analysis and repair-verification
pipeline. It collects failures into `WorkUnit` records, accepts a structured
unified diff from an explicitly selected adapter, validates that diff, and
tests it only in a disposable copy. The original project is not modified by
candidate evaluation.

The available OmniRush adapter is **human-mediated**. FaultLine can export a
constrained request and import a proposal JSON file returned by an actual
OmniRush session. It does not make an autonomous or synchronous agent call.

FaultLine also has a separate opt-in `repair-ai` command for Groq. Groq is
tested with mocks offline; a live run requires `GROQ_API_KEY` and is never
represented by the human-mediated OmniRush artifacts.

## 0:30–1:00 — Install and detect the broken fixture

From the repository root:

```sh
uv sync --locked
make demo
uv run --locked pytest benchmarks -q
uv run --locked faultline scan benchmarks --json-output scan.json
```

`make demo` is the repeatable offline walkthrough. It saves scan, request,
accepted-repair, rejection, and inconclusive reports under `demo-output/` and
returns nonzero if an expected pipeline status or source-integrity check is not
observed. It uses an explicit `offline_fixture` proposal because this checkout
does not contain an authentic proposal returned by an OmniRush session.

The benchmark tests are intentionally broken and are excluded from the
canonical `make test` command. In this checkout, the benchmark command
reported these three failures:

- `test_mutable_default_argument.py::test_each_call_starts_with_a_fresh_collection`
- `test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page`
- `test_unsafe_nested_dictionary_access.py::test_missing_nested_profile_uses_anonymous_fallback`

The scan prints the detected failures and their conservative `WorkUnit`s. Use
the pagination WorkUnit identifier from `scan.json` in the next commands; it
is shown as `<pagination-work-unit-id>` below rather than being invented here.

## 1:00–1:45 — Human-mediated OmniRush handoff

Export the constrained repair request:

```sh
uv run --locked faultline repair-export benchmarks \
  --work-unit <pagination-work-unit-id> \
  --output repair-request.json
```

The request contains the WorkUnit id, target source path/snippet, failing
pytest node, traceback/error context, authorized paths, and repair constraints.
The remaining human step is to give `repair-request.json` to an actual
OmniRush session and save its structured `PatchProposal` response as
`repair-proposal.json`. The proposal must include a root-cause explanation and
an existing-file unified diff; the session must not edit tests or claim runtime
success without verification.

If no live session is available, use the repository's explicit offline
fixture tests instead. That result must be labeled `offline_fixture`, not
reported as an OmniRush repair.

## 1:45–2:30 — Import, validate, and verify the pagination repair

When the human-produced proposal file exists, import it through the normal
worker and optionally request mutation evidence:

```sh
uv run --locked faultline repair-import benchmarks \
  --proposal repair-proposal.json \
  --work-unit <pagination-work-unit-id> \
  --json-output repair-report.json \
  --mutation-test \
  --max-mutants 5
```

The pagination fixture is broken because it uses:

```python
stop = start + page_size - 1
```

The accepted repair exercised by the repository tests changes that line to:

```python
stop = start + page_size
```

The worker does not treat a targeted pass as final acceptance. It applies the
candidate in a fresh temporary copy, parses changed Python files, runs the
failing node, verifies protected test-file inventory and SHA-256 hashes, then
compares a complete baseline suite with a complete candidate suite. The
acceptance arbiter returns `ACCEPTED` only when all mandatory gates pass. A
targeted pass before global verification is represented as
`PROVISIONALLY_SUCCESSFUL`, not final acceptance.

The repository test
`tests/test_day4_acceptance.py::test_worker_accepts_real_pagination_repair_after_global_regression_check`
asserts that this pagination proposal is accepted, that the targeted test and
test-integrity checks pass, that the original failure is resolved, and that the
original project copy remains unchanged. The test uses an explicit fixture
adapter; it is not live OmniRush evidence.

## 2:30–3:15 — Mutation testing is advisory

`--mutation-test` runs only after the mandatory acceptance gates return
`ACCEPTED`. FaultLine generates deterministic, target-function AST mutants,
runs selected previously passing pytest nodes against each mutant in a fresh
copy, and records `KILLED`, `SURVIVED`, or `INCONCLUSIVE` evidence. The
`--max-mutants` value bounds generation. Inconclusive mutants are excluded
from the kill-rate denominator; if there are no conclusive mutants, the score
is unavailable.

The focused mutation evidence is reproducible with:

```sh
uv run --locked pytest tests/test_mutation.py -q
```

The end-to-end `make demo` run prints the actual generated mutant counts,
outcomes, kill rate, and duration; it does not assume a fixed score. The
repository test suite covers deterministic mutation ids, comparison/arithmetic/numeric/
boolean/return-value operators, isolated mutant execution, killed and
surviving examples, timeout and invalid-source inconclusive outcomes, and the
accepted pagination integration. Mutation evidence is a quality signal; it
cannot turn a rejected or inconclusive repair into an accepted one, and it is
not a correctness proof.

## 3:15–3:45 — Show unsafe rejection and inconclusive verification

The patch parser is deliberately narrow. It rejects empty or malformed diffs,
unauthorized paths, traversal/absolute paths, test-file changes, protected
configuration changes, symlink destinations, file creation/deletion/rename,
binary patches, obvious test-suppression changes, and failed exact context.
Run the guardrail tests:

```sh
uv run --locked pytest tests/test_repair_primitives.py -q
```

The runtime integrity test also detects a candidate that changes a protected
test file during execution. Validation-stage unsafe diffs are recorded as
rejected; runtime test-integrity violations are recorded as a failed attempt,
so neither can be accepted.

Missing, timed-out, malformed, or collection-error evidence is not treated as
a pass. The baseline-timeout case is covered by
`tests/test_day4_acceptance.py::test_worker_reports_inconclusive_when_baseline_times_out`.
Run the regression evidence suite with:

```sh
uv run --locked pytest tests/test_regression.py -q
```

The expected classification in the timeout case is `INCONCLUSIVE` with
`evidence_complete` false. A confirmed newly failing test, by contrast, is a
`REJECTED` global-regression result.

## Current Day 3 batch behavior

The branch also contains bounded parallel orchestration around the single
WorkUnit worker:

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

The input is a WorkUnit list or a scan report containing `work_units`. Results
are returned in input order; active workers are bounded; one worker exception
is isolated from the others; and the aggregate report records per-WorkUnit
success, failure, rejection, cancellation, timeout, duration, and state
history. A proposal directory with `--backend fixture` is an explicit offline
fixture path, not an agent call. The batch command does not expose the
single-WorkUnit `--mutation-test` option.

The orchestration tests are:

```sh
uv run --locked pytest tests/test_day3_orchestration.py -q
```

## Optional Groq walkthrough

When a Groq key is available, set it in the process environment and run the
provider through the same guarded worker:

```powershell
$env:GROQ_API_KEY = "gsk_..."
uv run --locked faultline repair-ai benchmarks `
  --provider groq `
  --work-unit <pagination-work-unit-id> `
  --proposal-output groq-proposal.json `
  --json-output groq-repair-report.json `
  --mutation-test `
  --max-mutants 5
```

For a non-executing proposal handoff, add `--proposal-only`. The expected
report shape uses `execution_mode: "groq_automated"`; `ACCEPTED` still comes
only from FaultLine's existing validation, integrity, regression, and arbiter
gates. If no key is configured, run the mocked provider suite instead:

```sh
uv run --locked pytest tests/test_groq_provider.py tests/test_repair_ai.py -q
```

## Limitations and remaining manual steps

- A person must select or identify the pagination WorkUnit, review the exported
  request, use an actual OmniRush session if desired, save its proposal JSON,
  and run `repair-import`.
- No live OmniRush activity, external user, deployment, or benchmark
  performance result is represented by the repository tests.
- The local temporary-copy runner is not a hardened OS sandbox. It does not
  enforce network isolation, CPU/memory quotas, process limits, or a non-root
  user; use explicitly trusted local projects and fixtures.
- Batch worker lifecycle state is in memory and is not durable across restart.
  Python threads cannot be forcefully stopped, so active cancellation and
  timeout handling depend on cooperative worker checks.
- Mutation testing is local, bounded, target-scoped, and advisory.
- Groq repair is opt-in, requires a runtime API key, and has no live result in
  this repository unless the operator runs it directly. The SDK provider never
  receives shell access and never modifies the original project.

For the complete offline check, use:

```sh
make test
```

In the final verification checkout this command completed with `114 passed` and two dependency
deprecation warnings. The intentionally failing benchmark suite is separate
from that command.
