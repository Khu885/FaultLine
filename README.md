# FaultLine

FaultLine is a Day 1 deterministic debugging-analysis CLI for the OmniRush
Frontier Buildathon 2026, Developer Tools & Open Source track. It runs a target
project's pytest suite in a subprocess, parses `pytest-json-report`, localizes
traceback frames with AST analysis, and groups failures conservatively by
source file. It uses no APIs or OmniRush credentials.

Day 1 intentionally does **not** implement LLM integration, patching, repair
agents, a dashboard, or mutation testing. FaultLine reports evidence; it does
not invent a fix.

## Setup

Prerequisites:

- Python **3.12.15**, pinned in `.python-version` (the package supports 3.11+)
- uv 0.13.0 or newer
- GNU Make
- Docker Engine, optionally

Install from the lockfile:

```sh
uv sync --locked
```

## Scan

```sh
uv run --locked faultline scan path/to/project
uv run --locked faultline scan path/to/project --json-output report.json
```

FaultLine invokes `sys.executable -m pytest` with `pytest-json-report`, an
explicit timeout, and structured report parsing. It completes successfully
when target tests fail, while distinguishing test failures from collection
errors and missing or malformed reports.

Each failure includes its test node ID, exception type when available, message,
traceback, source path, line, enclosing function or method, source snippet, and
localization confidence. Frames outside the target project or without a
resolvable AST location are reported as unresolved with a reason. Test
assertion frames are distinguished from likely application-source frames.

Failures with the same resolved source file are grouped into one work unit.
This is conservative: grouping does not guarantee a shared root cause, and
different files are not guaranteed to be independent.

## Tests

`make test` is the canonical offline unit-test command and excludes the
intentionally broken benchmark suite:

```sh
make test
```

## Benchmarks

`benchmarks/` contains exactly three intentionally broken examples and tests:

1. off-by-one pagination;
2. mutable default argument;
3. unsafe nested dictionary access.

Run them explicitly:

```sh
uv run --locked pytest benchmarks -q
```

The expected result is exactly **3 failed** tests. Assertions are intentionally
not weakened. The FaultLine scan itself still completes successfully:

```sh
uv run --locked faultline scan benchmarks --json-output benchmark-scan.json
```

## Docker

The Dockerfile uses pinned Python 3.12.15 and installs from `uv.lock`:

```sh
docker build -t faultline .
docker run --rm faultline make test
```
