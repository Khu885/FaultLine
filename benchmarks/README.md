# Benchmark suite

These three examples are intentionally broken fixtures for the Buildathon benchmark
harness. Each pytest test states the behavior that a correct implementation should
provide, so the suite must fail until the corresponding bug is fixed:

- `test_off_by_one_pagination.py` catches a page slice that omits its last item.
- `test_mutable_default_argument.py` catches a list default that leaks state across calls.
- `test_unsafe_nested_dictionary_access.py` catches direct nested indexing when data is missing.

Run only the benchmark tests with:

```sh
uv run --locked pytest benchmarks/
```

The expected result is exactly **3 failed** tests. The primary unit-test command,
`make test`, discovers only `tests/` and therefore excludes these deliberate failures.
