"""Terminal-only rendering helpers for unified diffs."""

from __future__ import annotations

import os
from typing import Literal

from rich.console import Console
from rich.text import Text


DiffLineStyle = Literal["red", "green", "blue", "cyan"] | None


def classify_diff_line(line: str) -> DiffLineStyle:
    """Return the Git-style terminal color for one diff line."""
    if line.startswith(("---", "+++")):
        return "blue"
    if line.startswith("@@"):
        return "cyan"
    if line.startswith("-"):
        return "red"
    if line.startswith("+"):
        return "green"
    return None


def _color_enabled(console: Console | None) -> bool:
    """Enable color only when the output destination can represent it."""
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if console is None:
        return False
    return bool(console.is_terminal and console.color_system)


def render_unified_diff(diff: str, *, console: Console | None = None) -> Text:
    """Build a Rich Text diff without changing the underlying patch text.

    Returning ``Text`` even for the plain path prevents Rich markup in source
    context (for example ``items[0]``) from being interpreted as terminal
    markup.  JSON reports never call this function and therefore remain
    completely unaffected.
    """
    rendered = Text()
    color_enabled = _color_enabled(console)
    for line in diff.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        style = classify_diff_line(content) if color_enabled else None
        rendered.append(content, style=style)
        if newline:
            rendered.append(newline)
    return rendered


__all__ = ["DiffLineStyle", "classify_diff_line", "render_unified_diff"]
