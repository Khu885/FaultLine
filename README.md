# FaultLine

FaultLine is a deterministic pytest failure-analysis CLI. It runs a target project's tests in an isolated subprocess, parses `pytest-json-report`, localizes traceback frames with the AST localizer, and groups failures conservatively by source file. It has no network calls and needs no OmniRush credentials.

## Prerequisites

- Python **3.12.15** (the reproducible runtime is recorded in `.python-version`; the package supports Python 3.11+)
- [uv](https://docs.astral.sh/uv/) **0.13.0 or newer**
- GNU Make for the canonical test command
- Docker Engine, if using the container workflow

## Installation

From the repository root:

```sh
uv sync --locked
```

This creates the project-local `.venv` and installs exactly what `uv.lock` specifies. No credentials or external services are needed.

## Running FaultLine

Scan a project containing pytest tests:

```sh
uv run --locked faultline scan path/to/project
```

The command completes successfully even when target tests fail. Write the structured `ScanReport` JSON for automation with:

```sh
uv run --locked faultline scan path/to/project --json-output report.json
```

The report distinguishes failed tests from collection errors, handles empty suites, and includes conservative source-file work units.

## Tests

`make test` is the one canonical offline test command. It runs the focused unit tests under `tests/` and deliberately does not run the intentionally broken benchmark suite:

```sh
make test
```

## Docker

Build the image with the pinned Python runtime and locked project dependencies:

```sh
docker build -t faultline .
```

The default container command runs the unit tests. The explicit form below is equivalent and is useful for CI:

```sh
docker run --rm faultline make test
```

To scan a project in the container, override the default command:

```sh
docker run --rm -v "$PWD:/scan:ro" faultline faultline scan /scan --json-output /tmp/report.json
```

## Benchmark expectations

`benchmarks/` contains exactly three deliberately broken examples used by the Buildathon harness. Their failures are expected and are excluded from `make test`. To observe them explicitly:

```sh
uv run --locked pytest benchmarks -q
```
