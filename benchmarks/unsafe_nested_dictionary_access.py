"""Broken nested-dictionary example: missing keys raise instead of using a fallback."""


def display_name(payload: dict) -> str:
    """Read a user's display name (intentionally broken for the benchmark)."""
    return payload["user"]["profile"]["display_name"]
