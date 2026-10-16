"""Protected test-file inventory and SHA-256 integrity checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .models import TestIntegrityResult, TestIntegritySnapshot


def discover_test_files(project_root: str | Path) -> list[Path]:
    """Discover pytest test files deterministically, without following links."""
    root = Path(project_root).resolve()
    found: list[Path] = []
    for candidate in sorted(root.rglob("*.py")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        parts = relative.parts
        if "tests" in parts or candidate.name.startswith("test_") or candidate.name.endswith("_test.py"):
            found.append(candidate)
    return found


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_test_files(project_root: str | Path) -> TestIntegritySnapshot:
    """Record relative test paths and SHA-256 contents."""
    root = Path(project_root).resolve()
    return TestIntegritySnapshot(
        files={path.relative_to(root).as_posix(): _digest(path) for path in discover_test_files(root)}
    )


def verify_test_integrity(
    project_root: str | Path,
    before: TestIntegritySnapshot,
) -> TestIntegrityResult:
    """Detect changed, removed, or newly-created protected test files."""
    after = snapshot_test_files(project_root)
    before_paths = set(before.files)
    after_paths = set(after.files)
    changed = sorted(path for path in before_paths & after_paths if before.files[path] != after.files[path])
    removed = sorted(before_paths - after_paths)
    added = sorted(after_paths - before_paths)
    passed = not changed and not removed and not added
    reason = None
    if not passed:
        details: list[str] = []
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        if removed:
            details.append(f"removed: {', '.join(removed)}")
        if added:
            details.append(f"added: {', '.join(added)}")
        reason = "test integrity violation (" + "; ".join(details) + ")"
    return TestIntegrityResult(
        passed=passed,
        changed_paths=changed,
        removed_paths=removed,
        added_paths=added,
        reason=reason,
    )
