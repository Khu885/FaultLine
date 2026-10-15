.PHONY: test

test:
	uv run --locked python -m unittest discover -s tests -p 'test_*.py' -v
