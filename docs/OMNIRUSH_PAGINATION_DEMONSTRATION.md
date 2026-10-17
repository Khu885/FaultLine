# Authentic OmniRush pagination demonstration

This document records the authentic proposal handoff performed in the current
OmniRush session. It is deliberately separate from the deterministic
`offline_fixture` demonstrations in `scripts/demo.py` and
`tests/test_day4_acceptance.py`.

## Provenance

- Repository branch at the start: `feature/faultline-day3`.
- Starting commit: `4196555 fix: ignore stale caches in disposable copies`.
- OmniRush action: this current OmniRush session read the exported
  `RepairRequest`, analyzed the traceback and source snippet, identified the
  exclusive Python slice endpoint defect, and wrote the structured proposal at
  `demo-artifacts/omnirush-pagination-patch-proposal.json`.
- FaultLine action: scan, request export, proposal JSON validation, patch
  validation/application in a disposable copy, targeted pytest execution,
  SHA-256 test-integrity verification, global regression comparison, arbiter
  decision, and optional mutation testing.
- No direct automated OmniRush API or CLI call was used.
- No offline fixture proposal was relabeled as OmniRush output.

The proposal is preserved unchanged as a separate artifact. Its SHA-256 at
verification time was:

```text
5403bc38601a76ecdc7ba08d2b8d17ae336a48ca40d6c00dd422a2a87cbf0274
```

## Exact commands

### 1. Detect the failures

```sh
uv run --locked faultline scan benchmarks \
  --json-output demo-output/authentic-scan-report.json
```

Observed result: 3 failing tests and 3 WorkUnits. The pagination WorkUnit was
`2cccbe7dd1a50066`.

### 2. Export the constrained request

```sh
uv run --locked faultline repair-export benchmarks \
  --work-unit 2cccbe7dd1a50066 \
  --output demo-output/authentic-pagination-repair-request.json
```

The exported request is preserved at:

```text
demo-artifacts/omnirush-pagination-repair-request.json
```

It matches the request produced by `repair-export` exactly after JSON decoding.
The request authorized only `off_by_one_pagination.py` and required
the proposal to avoid test/configuration changes and runtime claims.

### 3. OmniRush proposal generation

The current OmniRush session analyzed the exported request and generated this
proposal artifact:

```text
demo-artifacts/omnirush-pagination-patch-proposal.json
```

The proposal makes the minimal source-only change:

```diff
--- a/off_by_one_pagination.py
+++ b/off_by_one_pagination.py
@@ -6,3 +6,3 @@
     start = page * page_size
-    stop = start + page_size - 1
+    stop = start + page_size
     return items[start:stop]
```

Its `execution_mode` is `omnirush_human_mediated`, and its diagnostic notes
explicitly state that FaultLine must verify it.

### 4. Import and verify the authentic proposal

```sh
uv run --locked faultline repair-import benchmarks \
  --proposal demo-artifacts/omnirush-pagination-patch-proposal.json \
  --work-unit 2cccbe7dd1a50066 \
  --json-output demo-output/authentic-omnirush-pagination-repair-report.json \
  --max-attempts 1 \
  --timeout 10 \
  --mutation-test \
  --max-mutants 5
```

The raw structured report is saved at:

```text
demo-output/authentic-omnirush-pagination-repair-report.json
```

That path is ignored because the raw report contains machine-specific absolute
paths from pytest and temporary copies. A path-redacted evidence copy is
preserved for review at:

```text
demo-artifacts/omnirush-pagination-repair-report.redacted.json
```

The redacted copy preserves the result fields and classifications; it does not
alter the proposal or verification decision.

## Actual final evidence

The final imported proposal produced:

| Evidence | Result |
|---|---|
| Execution mode | `omnirush_human_mediated` |
| Targeted test | passed |
| SHA-256 test integrity | passed |
| Global regression | passed |
| Arbiter decision | `ACCEPTED` |
| Mutants generated | 5 |
| Mutants killed | 4 |
| Mutants survived | 1 |
| Mutants inconclusive | 0 |
| Mutation kill rate | 80.00% |
| Mutation duration | approximately 31.75 seconds |

The surviving-mutant warning remained advisory and did not affect the
mandatory `ACCEPTED` decision.

## Integration issue found and fixed during the authentic run

The first import attempt was intentionally not hidden. FaultLine correctly
returned `REJECTED` because the baseline report used package-prefixed node IDs
such as `benchmarks/test_off_by_one_pagination.py::...`, while the disposable
candidate report used `test_off_by_one_pagination.py::...`. The resulting report
is retained locally as:

```text
demo-output/authentic-omnirush-pagination-initial-rejection-report.json
```

The proposal was not changed. The issue was fixed in
`src/faultline/regression.py` by normalizing a project-name wrapper only when
the shorter path exists under the project root. A regression test was added in
`tests/test_regression.py`. The exact same proposal was then imported again and
returned `ACCEPTED`.

## Original benchmark integrity

After the accepted run:

```sh
git diff --exit-code -- benchmarks
```

passed. The original benchmark source and test files were not modified.

## Full-suite verification

After the integration fix:

```sh
uv run --locked pytest tests -q
make test
```

both completed with **91 passed** and the existing two Typer/Click deprecation
warnings. The benchmark suite remains intentionally failing with 3 failures.
