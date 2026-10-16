"""Run pytest in a subprocess and parse pytest-json-report safely."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .localizer import infer_imported_application_location, localize_traceback
from .models import FailureRecord, ScanReport
from .planner import group_work_units


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _traceback_text(phase: Mapping[str, Any]) -> str:
    value = phase.get("longrepr")
    if value is not None:
        return _text(value)
    frames = phase.get("traceback")
    if isinstance(frames, list):
        return "\n".join(_text(frame.get("message", frame)) if isinstance(frame, Mapping) else _text(frame) for frame in frames)
    return _text(frames)


def _frames(phase: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    frames = phase.get("traceback", [])
    return [frame for frame in frames if isinstance(frame, Mapping)] if isinstance(frames, list) else []


def _failure_from_test(test: Mapping[str, Any], phase_name: str, phase: Mapping[str, Any], project_path: Path) -> FailureRecord:
    locations = [{"filename": frame.get("filename", frame.get("file", frame.get("path"))), "lineno": frame.get("lineno", frame.get("line", frame.get("line_number")))} for frame in _frames(phase)]
    location = localize_traceback(locations, project_path) if locations else None
    if location is not None and location.category != "application":
        inferred = infer_imported_application_location(location, project_path)
        if inferred is not None:
            location = inferred
    return FailureRecord(
        nodeid=_text(test.get("nodeid", "<unknown>")),
        test_name=_text(test.get("nodeid")) or None,
        message=_text(phase.get("longrepr", phase.get("message", ""))),
        traceback=_traceback_text(phase),
        source_location=location,
        phase=phase_name,
    )


def parse_report(payload: Mapping[str, Any], project_path: str | Path, *, exit_code: int = 0) -> ScanReport:
    """Parse a decoded pytest-json-report document without raising on bad shapes."""
    root = Path(project_path).resolve()
    failures: list[FailureRecord] = []
    tests = payload.get("tests", [])
    if not isinstance(tests, list):
        tests = []
    for test in tests:
        if not isinstance(test, Mapping):
            continue
        for phase_name in ("setup", "call", "teardown"):
            phase = test.get(phase_name)
            if isinstance(phase, Mapping) and phase.get("outcome") == "failed":
                failures.append(_failure_from_test(test, phase_name, phase, root))
                break
    summary = payload.get("summary")
    test_count = summary.get("total", len(tests)) if isinstance(summary, Mapping) else len(tests)
    try:
        test_count = int(test_count)
    except (TypeError, ValueError):
        test_count = len(tests)
    collection_error = None
    collectors = payload.get("collectors", [])
    if isinstance(collectors, list):
        bad = [item for item in collectors if isinstance(item, Mapping) and item.get("outcome") in {"failed", "error"}]
        if bad:
            collection_error = "pytest collection failed"
    return ScanReport(
        project_path=str(root), failures=failures, work_units=group_work_units(failures),
        collection_error=collection_error, test_count=test_count, exit_code=exit_code,
    )


def parse_report_file(report_path: str | Path, project_path: str | Path, *, exit_code: int = 0) -> ScanReport:
    """Read a report file; malformed and missing files become report metadata."""
    root = Path(project_path).resolve()
    try:
        raw = Path(report_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError:
        return ScanReport(project_path=str(root), report_error="pytest report was not produced", exit_code=exit_code)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ScanReport(project_path=str(root), report_error=f"pytest report is malformed: {exc.__class__.__name__}", exit_code=exit_code)
    if not isinstance(payload, Mapping):
        return ScanReport(project_path=str(root), report_error="pytest report is malformed: expected an object", exit_code=exit_code)
    return parse_report(payload, root, exit_code=exit_code)


def collect_project(project_path: str | Path, *, timeout: float = 60.0) -> ScanReport:
    """Run pytest and return a report even when tests fail or collection breaks."""
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return ScanReport(project_path=str(root), collection_error="project path is not a directory", exit_code=2)
    with tempfile.TemporaryDirectory(prefix="faultline-") as temporary:
        report_path = Path(temporary) / "pytest-report.json"
        command = [sys.executable, "-m", "pytest", "--json-report", f"--json-report-file={report_path}", str(root)]
        try:
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return ScanReport(project_path=str(root), collection_error=f"pytest timed out after {timeout:g} seconds", exit_code=124)
        report = parse_report_file(report_path, root, exit_code=completed.returncode)
        if completed.returncode == 2 and report.collection_error is None:
            detail = completed.stderr.strip() or completed.stdout.strip() or "pytest collection failed"
            report.collection_error = detail.splitlines()[-1]
        if completed.returncode == 5 and report.report_error == "pytest report was not produced":
            report.report_error = None
            report.test_count = 0
        return report


# Public aliases keep the pipeline entry points easy to discover.
collect_tests = collect_project
run_pytest = collect_project
parse_json_report = parse_report_file
