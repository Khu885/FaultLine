"""AST-based traceback source localization."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import SourceLocation


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_TEST_FILE = re.compile(r"(?:^|[_/])test(?:[_/]|[.]|$)|(?:^|[_/])tests(?:[_/]|$)", re.I)
_ASSERT_METHOD = re.compile(r"\b(?:assert[A-Z]\w*|pytest\.raises)\s*\(")


def _unresolved(reason: str, *, path: str | None = None, line: int | None = None) -> SourceLocation:
    return SourceLocation(status="unresolved", path=path, line=line, reason=reason, confidence="unresolved")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_path(source_path: str | Path, target_project: str | Path) -> tuple[Path | None, Path, str | None]:
    root = Path(target_project).expanduser().resolve(strict=False)
    raw = Path(source_path).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    normalized = candidate.resolve(strict=False)
    if not _inside(normalized, root):
        return None, root, "source file is outside the target project"
    return normalized, root, None


def _node_start(node: ast.AST) -> int:
    starts = [getattr(node, "lineno", 0)]
    starts.extend(getattr(decorator, "lineno", 0) for decorator in getattr(node, "decorator_list", ()))
    return min((line for line in starts if line), default=0)


def _node_end(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 0))


def _qualified_name(node: ast.AST, parents: Mapping[int, ast.AST]) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, _FUNCTION_NODES):
            names.append(current.name)
        elif isinstance(current, ast.ClassDef):
            names.append(current.name)
        current = parents.get(id(current))
    return ".".join(reversed(names))


def _walk_with_parents(tree: ast.AST) -> tuple[list[ast.AST], dict[int, ast.AST]]:
    parents: dict[int, ast.AST] = {}
    nodes: list[ast.AST] = []
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            if isinstance(child, _FUNCTION_NODES):
                nodes.append(child)
    return nodes, parents


def _is_test_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    name = path.name
    return bool(_TEST_FILE.search(relative) or name.startswith("test_") or name.endswith("_test.py"))


def _assertion_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            lines.update(range(node.lineno, _node_end(node) + 1))
    return lines


def localize_source_line(source_path: str | Path, line_number: int, target_project: str | Path) -> SourceLocation:
    """Locate the smallest enclosing function or method for one source line."""

    path_text = str(source_path)
    if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
        return _unresolved("traceback line number is invalid", path=path_text)

    try:
        path, root, path_error = _normalize_path(source_path, target_project)
    except (OSError, TypeError, ValueError):
        return _unresolved("source path could not be normalized", path=path_text, line=line_number)
    if path_error or path is None:
        return _unresolved(path_error or "source path could not be normalized", path=path_text, line=line_number)
    if not path.is_file():
        return _unresolved("source file does not exist", path=str(path), line=line_number)

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _unresolved(f"source file could not be read: {exc.__class__.__name__}", path=str(path), line=line_number)
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError, TypeError) as exc:
        return _unresolved(f"source is not valid Python: {exc.__class__.__name__}", path=str(path), line=line_number)

    source_lines = source.splitlines()
    if line_number > len(source_lines):
        return _unresolved("traceback line is outside the source file", path=str(path), line=line_number)

    functions, parents = _walk_with_parents(tree)
    enclosing = [node for node in functions if _node_start(node) <= line_number <= _node_end(node)]
    if not enclosing:
        return _unresolved("no enclosing function or method", path=str(path), line=line_number)
    node = min(enclosing, key=lambda item: (-_node_start(item), _node_end(item) - _node_start(item)))
    is_test = _is_test_path(path, root)
    assertions = _assertion_lines(tree)
    line_text = source_lines[line_number - 1]
    is_assertion = line_number in assertions or bool(re.search(r"\bassert\b", line_text)) or bool(_ASSERT_METHOD.search(line_text))
    category = "test_assertion" if is_test and is_assertion else "test" if is_test else "application"
    return SourceLocation(
        status="resolved", path=str(path), line=line_number, function=_qualified_name(node, parents),
        source_snippet=line_text.strip(),
        confidence="medium" if category == "test_assertion" else "high",
        category=category, is_test=is_test, is_assertion=is_assertion,
    )


localize_traceback_line = localize_source_line
locate_source_line = localize_source_line
localize = localize_source_line


def _frame_values(frame: Any) -> tuple[str | Path | None, int | None]:
    if isinstance(frame, Mapping):
        path = frame.get("filename", frame.get("file", frame.get("path")))
        line = frame.get("lineno", frame.get("line", frame.get("line_number")))
        return path, line
    path = getattr(frame, "filename", getattr(frame, "file", getattr(frame, "path", None)))
    line = getattr(frame, "lineno", getattr(frame, "line", getattr(frame, "line_number", None)))
    if path is not None:
        return path, line
    if isinstance(frame, Sequence) and not isinstance(frame, (str, bytes)) and len(frame) >= 2:
        return frame[0], frame[1]
    return None, None


def localize_frame(frame: Any, target_project: str | Path) -> SourceLocation:
    """Localize a traceback ``FrameSummary``, mapping, or ``(path, line)`` pair."""

    path, line = _frame_values(frame)
    if path is None:
        return _unresolved("traceback frame has no source path")
    if not isinstance(line, int) or isinstance(line, bool):
        return _unresolved("traceback frame has no valid line number", path=str(path))
    return localize_source_line(path, line, target_project)


def localize_traceback(frames: Iterable[Any], target_project: str | Path) -> SourceLocation:
    """Choose a likely fault frame, preferring application code over test assertions."""

    locations = [localize_frame(frame, target_project) for frame in frames]
    resolved = [location for location in locations if location.status == "resolved"]
    if not resolved:
        return locations[-1] if locations else _unresolved("traceback contains no frames")
    application = [location for location in resolved if location.category == "application"]
    if application:
        return application[-1]
    non_assertion_test = [location for location in resolved if location.category == "test"]
    return (non_assertion_test or resolved)[-1]


resolve_traceback = localize_traceback


def _module_path(module: str, root: Path) -> Path | None:
    """Resolve a normal Python import to a source file inside ``root``."""
    parts = module.split(".")
    candidates = [parts]
    # A project can be scanned from inside a package directory, while tests
    # still import that directory by its package name (e.g. ``benchmarks``).
    if parts and parts[0] == root.name:
        candidates.append(parts[1:])
    for candidate_parts in candidates:
        if not candidate_parts:
            continue
        candidate = root.joinpath(*candidate_parts).with_suffix(".py")
        if candidate.is_file():
            return candidate
        package_init = root.joinpath(*candidate_parts, "__init__.py")
        if package_init.is_file():
            return package_init
    return None


def infer_imported_application_location(
    test_location: SourceLocation,
    target_project: str | Path,
) -> SourceLocation | None:
    """Infer an application function for assertion-only pytest tracebacks.

    Pytest often reports only the assertion frame when a function returns a
    wrong value.  This deliberately narrow fallback follows a direct
    ``from module import function`` used by the test, and only returns a
    definition that is present in a source file under the project root.  It
    does not guess from arbitrary text or leave the target project.
    """
    if test_location.status != "resolved" or not test_location.path or not test_location.is_test:
        return None
    root = Path(target_project).expanduser().resolve(strict=False)
    test_path = Path(test_location.path).resolve(strict=False)
    if not _inside(test_path, root) or not test_path.is_file():
        return None
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError):
        return None
    imports: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if alias.name != "*":
                    imports.append((node.module, alias.name))
    for module, symbol in imports:
        application_path = _module_path(module, root)
        if application_path is None or application_path == test_path:
            continue
        try:
            application_tree = ast.parse(
                application_path.read_text(encoding="utf-8"), filename=str(application_path)
            )
        except (OSError, UnicodeError, SyntaxError, ValueError, TypeError):
            continue
        for node in ast.walk(application_tree):
            if isinstance(node, _FUNCTION_NODES) and node.name == symbol:
                return SourceLocation(
                    status="resolved",
                    path=str(application_path),
                    line=getattr(node, "lineno", None),
                    function=symbol,
                    category="application",
                    is_test=False,
                    is_assertion=False,
                )
    return None
