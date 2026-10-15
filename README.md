# FaultLine

FaultLine is a deterministic, dependency-light CLI that scans a file or source tree for common fault markers (`TODO`, `FIXME`, `BUG`, and `NotImplementedError`). It has no network calls and needs no OmniRush credentials.

## Prerequisites

- Python **3.12.15** (the required version is recorded in `.python-version`)
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

Scan a file or directory:

```sh
uv run --locked faultline scan path/to/source
```

The command exits with status `0` when no markers are found and `1` when findings are present. Use JSON output for automation:

```sh
uv run --locked faultline scan --json path/to/source
```

## Tests

`make test` is the one canonical test command. It runs FaultLine's unit tests under `tests/` and deliberately does **not** run the benchmark suite:

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

To run the CLI in the container, override the default command:

```sh
docker run --rm -v "$PWD:/scan:ro" faultline faultline scan /scan
```

## Benchmark expectations

`benchmarks/` contains a deliberately failing baseline used by the Buildathon harness. Its failure is expected and is not a product regression; it is excluded from `make test`. To observe that expected failure explicitly:

```sh
uv run --locked python -m unittest discover -s benchmarks -p 'test_*.py'
```

That command should exit non-zero with the message `intentional benchmark baseline failure`.
