"""Controlled unified-diff validation and application.

This module deliberately does not invoke a shell or a system ``patch``
executable.  It supports the ordinary text unified diffs produced for one
existing Python source file and applies hunks only after exact context checks.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .models import PatchProposal, PatchValidationResult


class PatchRejectedError(ValueError):
    """Raised when a candidate diff violates a patch guardrail."""


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)
_PROTECTED_CONFIG = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "setup.py"}
_SUPPRESSION = re.compile(
    r"(?:pytest\s*\.\s*(?:skip|xfail)|pytestmark\s*=|@pytest\.mark\.(?:skip|xfail)|"
    r"unittest\.skip|@unittest\.skip|--(?:ignore|disable-warnings)|filterwarnings\s*=)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _FilePatch:
    old_path: str
    new_path: str
    hunks: tuple[_Hunk, ...]


def _is_file_header(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and lines[index].startswith("--- ")
        and lines[index + 1].startswith("+++ ")
    )


def _clean_diff_path(value: str) -> str:
    value = value.strip().split("\t", 1)[0]
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    if not value or value == "/dev/null" or "\\" in value:
        raise PatchRejectedError("absolute, binary, or unsupported diff path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise PatchRejectedError("path traversal or absolute patch path is not allowed")
    return path.as_posix()


def _parse_diff(diff: str) -> list[_FilePatch]:
    if not diff or not diff.strip():
        raise PatchRejectedError("empty patch")
    if "GIT binary patch" in diff or "Binary files " in diff:
        raise PatchRejectedError("binary patches are not supported")
    lines = diff.splitlines()
    patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise PatchRejectedError("malformed unified diff: missing --- header")
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise PatchRejectedError("malformed unified diff: missing +++ header")
        old_path = _clean_diff_path(lines[index][4:])
        new_path = _clean_diff_path(lines[index + 1][4:])
        if old_path != new_path:
            raise PatchRejectedError("file creation, deletion, and renames are not allowed")
        index += 2
        hunks: list[_Hunk] = []
        while index < len(lines) and not _is_file_header(lines, index):
            match = _HUNK_HEADER.match(lines[index])
            if not match:
                raise PatchRejectedError("malformed unified diff: invalid hunk header")
            old_count = int(match.group("old_count") or "1")
            new_count = int(match.group("new_count") or "1")
            index += 1
            hunk_lines: list[str] = []
            old_seen = new_seen = 0
            while index < len(lines) and not _is_file_header(lines, index) and not _HUNK_HEADER.match(lines[index]):
                line = lines[index]
                if line == r"\ No newline at end of file":
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise PatchRejectedError("malformed unified diff: invalid hunk line")
                hunk_lines.append(line)
                if line[0] in " -":
                    old_seen += 1
                if line[0] in " +":
                    new_seen += 1
                index += 1
            if old_seen != old_count or new_seen != new_count:
                raise PatchRejectedError("malformed unified diff: hunk line counts do not match")
            hunks.append(_Hunk(int(match.group("old")), old_count, int(match.group("new")), new_count, tuple(hunk_lines)))
        if not hunks:
            raise PatchRejectedError("malformed unified diff: no hunks")
        patches.append(_FilePatch(old_path, new_path, tuple(hunks)))
    if not patches:
        raise PatchRejectedError("malformed unified diff")
    return patches


def _is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _check_paths(
    patches: Iterable[_FilePatch],
    *,
    target_file: str,
    authorized_paths: set[str],
    protected_paths: set[str],
) -> None:
    target = _clean_diff_path(target_file)
    for patch in patches:
        if patch.new_path != target or patch.old_path != target:
            raise PatchRejectedError("patch modifies a file outside the requested target")
        if patch.new_path not in authorized_paths:
            raise PatchRejectedError(f"patch path is not authorized: {patch.new_path}")
        if patch.new_path in protected_paths or _is_test_path(patch.new_path):
            raise PatchRejectedError("test-file modification is not allowed")
        if PurePosixPath(patch.new_path).name in _PROTECTED_CONFIG:
            raise PatchRejectedError("protected project configuration cannot be modified")


def _apply_file_patch(path: Path, patch: _FilePatch) -> None:
    if path.is_symlink():
        raise PatchRejectedError("unsafe symbolic-link patch destination")
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PatchRejectedError(f"target source file cannot be read: {exc}") from exc
    had_trailing_newline = original.endswith("\n")
    source_lines = original.splitlines()
    result: list[str] = []
    cursor = 0
    for hunk in patch.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if start < cursor or start > len(source_lines):
            raise PatchRejectedError("patch hunk is outside the target file")
        result.extend(source_lines[cursor:start])
        cursor = start
        for line in hunk.lines:
            marker, content = line[0], line[1:]
            if marker == " ":
                if cursor >= len(source_lines) or source_lines[cursor] != content:
                    raise PatchRejectedError("patch context does not match target file")
                result.append(content)
                cursor += 1
            elif marker == "-":
                if cursor >= len(source_lines) or source_lines[cursor] != content:
                    raise PatchRejectedError("patch deletion does not match target file")
                cursor += 1
            elif marker == "+":
                if _SUPPRESSION.search(content):
                    raise PatchRejectedError("patch contains an obvious test-suppression mechanism")
                result.append(content)
    result.extend(source_lines[cursor:])
    updated = "\n".join(result)
    if had_trailing_newline or result:
        updated += "\n"
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
            raise PatchRejectedError(f"target source file cannot be written: {exc}") from exc


def _contains_symlink(root: Path, destination: Path) -> bool:
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def validate_and_apply_patch(
    proposal: PatchProposal,
    project_root: str | Path,
    *,
    authorized_paths: Iterable[str],
    protected_paths: Iterable[str] = (),
) -> PatchValidationResult:
    """Validate and apply a proposal inside a disposable project copy."""
    try:
        if proposal.status != "proposed":
            raise PatchRejectedError("proposal status is not proposed")
        patches = _parse_diff(proposal.unified_diff)
        authorized = {_clean_diff_path(path) for path in authorized_paths}
        protected = {_clean_diff_path(path) for path in protected_paths}
        _check_paths(
            patches,
            target_file=proposal.target_file,
            authorized_paths=authorized,
            protected_paths=protected,
        )
        root = Path(project_root).resolve()
        for patch in patches:
            destination = root / Path(*PurePosixPath(patch.new_path).parts)
            resolved_parent = destination.parent.resolve()
            if root not in (resolved_parent, *resolved_parent.parents):
                raise PatchRejectedError("patch destination escapes the project copy")
            if _contains_symlink(root, destination):
                raise PatchRejectedError("unsafe symbolic-link patch destination")
            _apply_file_patch(destination, patch)
            if destination.suffix == ".py":
                try:
                    ast.parse(destination.read_text(encoding="utf-8"), filename=str(destination))
                except (OSError, UnicodeError, SyntaxError) as exc:
                    raise PatchRejectedError(f"patch introduces Python syntax error: {exc}") from exc
        return PatchValidationResult(valid=True, changed_paths=[patch.new_path for patch in patches])
    except PatchRejectedError as exc:
        reason = str(exc)
        return PatchValidationResult(
            valid=False,
            reason=reason,
            syntax_error=reason if "syntax" in reason else None,
        )
