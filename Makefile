.PHONY: test

test:
	uv run --locked pytest -q
