.PHONY: test demo

test:
	uv run --locked pytest -q

demo:
	uv run --locked python scripts/demo.py
