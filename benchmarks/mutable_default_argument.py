"""Broken mutable-default example: state leaks between calls."""


def append_item(item: str, collected: list[str] = []) -> list[str]:
    """Append an item to a collection (intentionally broken for the benchmark)."""
    collected.append(item)
    return collected
