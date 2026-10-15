"""Core scanning logic for FaultLine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_MARKERS = ("TODO", "FIXME", "BUG", "NotImplementedError")
SKIPPED_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


@dataclass(frozen=True)
class Finding:
    """One marker found in a source file."""

    path: str
    line: int
    marker: str
    text: str


def _iter_files(path: Path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise FileNotFoundError(path)
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and not any(part in SKIPPED_DIRECTORIES for part in candidate.parts):
            yield candidate


def scan_path(path: str | Path, markers: tuple[str, ...] = DEFAULT_MARKERS) -> list[Finding]:
    """Scan a file or directory and return deterministic marker findings."""
    root = Path(path)
    findings: list[Finding] = []
    for candidate in _iter_files(root):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_number, text in enumerate(lines, start=1):
            for marker in markers:
                if marker in text:
                    findings.append(Finding(str(candidate), line_number, marker, text.strip()))
    return findings
