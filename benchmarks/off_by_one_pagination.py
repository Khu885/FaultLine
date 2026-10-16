"""Broken pagination example: it skips the final item on every full page."""


def paginate(items: list[str], page: int, page_size: int) -> list[str]:
    """Return one page of items (intentionally broken for the benchmark)."""
    start = page * page_size
    stop = start + page_size - 1
    return items[start:stop]
