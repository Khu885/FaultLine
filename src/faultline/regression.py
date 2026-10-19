"""Global pytest regression verification for disposable candidate projects.

Day 4 verification deliberately lives beside, rather than inside, the Day 1-3
collector and repair worker.  The collector is optimized for finding failures;
this module keeps every stable pytest node outcome so a complete baseline can
be compared with a candidate run without changing the existing repair flow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import RegressionReport, RegressionTestResult, VerificationReport


_KNOWN_OUTCOMES: frozenset[str] = frozenset(
    {"passed", "failed", "skipped", "xfailed", "xpassed", "error", "unavailable"}
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_nodeid(value: Any, project_root: str | Path | None = None) -> str:
    """Normalize paths while keeping parametrization and selectors intact."""
    nodeid = _text(value).replace("\\", "/").strip()
    if project_root is None or not nodeid:
        return nodeid
    root = str(project_root).replace("\\", "/").rstrip("/")
    node_path, separator, selector = nodeid.partition("::")
    if node_path == root:
        node_path = "."
    elif node_path.startswith(root + "/"):
        node_path = node_path[len(root) + 1 :]
    else:
        # Pytest can include the project directory name when the directory is
        # also an importable package (for example ``benchmarks/test_app.py``),
        # while a disposable copy of the same project reports
        # ``test_app.py``.  Strip that wrapper only when the shorter path is an
        # actual file under the supplied project root; this avoids confusing a
        # legitimate nested directory with the root-name wrapper.
        root_path = Path(project_root).expanduser().resolve(strict=False)
        root_name = root_path.name.replace("\\", "/")
        prefix = root_name + "/"
        if node_path.startswith(prefix):
            candidate = root_path.joinpath(*node_path[len(prefix) :].split("/"))
            if candidate.exists():
                node_path = node_path[len(prefix) :]
    return node_path + (separator + selector if separator else "")


def _phase_outcome(test: Mapping[str, Any]) -> str | None:
    outcome = test.get("outcome")
    if isinstance(outcome, str) and outcome in _KNOWN_OUTCOMES:
        return outcome

    # pytest-json-report normally supplies a top-level outcome.  Deriving it
    # from phases makes hand-written/offline reports useful and identifies a
    # setup or teardown error as non-passing rather than silently passing it.
    phases = (test.get("setup"), test.get("call"), test.get("teardown"))
    phase_outcomes: list[str] = []
    for phase in phases:
        if not isinstance(phase, Mapping):
            continue
        phase_outcome = phase.get("outcome")
        if isinstance(phase_outcome, str) and phase_outcome in _KNOWN_OUTCOMES:
            phase_outcomes.append(phase_outcome)
    for outcome_name in ("error", "failed", "skipped", "xfailed", "xpassed", "passed"):
        if outcome_name in phase_outcomes:
            return outcome_name
    return None


def _phase_message(test: Mapping[str, Any]) -> str:
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if not isinstance(phase, Mapping):
            continue
        for key in ("longrepr", "message"):
            message = _text(phase.get(key)).strip()
            if message:
                return message
    return _text(test.get("longrepr", test.get("message", ""))).strip()


def _test_duration(test: Mapping[str, Any]) -> float:
    if "duration" in test:
        return _safe_float(test.get("duration"))
    return sum(
        _safe_float(phase.get("duration"))
        for phase in (test.get("setup"), test.get("call"), test.get("teardown"))
        if isinstance(phase, Mapping)
    )


def _unique_sorted(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


def _collector_detail(collector: Mapping[str, Any], project_root: str | Path | None = None) -> str:
    nodeid = _normalize_nodeid(collector.get("nodeid"), project_root)
    detail = _text(collector.get("longrepr", collector.get("message", ""))).strip()
    if detail:
        return f"{nodeid}: {detail}" if nodeid else detail
    return nodeid or "unknown collector"


def _collection_errors(
    payload: Mapping[str, Any], project_root: str | Path | None = None
) -> tuple[list[str], bool]:
    """Return collection diagnostics and whether the collector shape was valid."""
    collectors = payload.get("collectors")
    if collectors is None:
        return [], True
    if not isinstance(collectors, list):
        return [], False

    errors: list[str] = []
    valid = True
    for collector in collectors:
        if not isinstance(collector, Mapping):
            valid = False
            continue
        outcome = _text(collector.get("outcome")).lower()
        if outcome in {"failed", "error"}:
            errors.append(f"pytest collection failed: {_collector_detail(collector, project_root)}")
    return _unique_sorted(errors), valid


def _report_failure_reason(
    *,
    report_error: str | None,
    timed_out: bool,
    timeout_reason: str | None,
    incomplete_reasons: list[str],
    collection_errors: list[str],
) -> str | None:
    reasons: list[str] = []
    if report_error:
        reasons.append(report_error)
    if timed_out:
        reasons.append(timeout_reason or "pytest timed out")
    if incomplete_reasons:
        reasons.append("incomplete pytest report: " + "; ".join(_unique_sorted(incomplete_reasons)))
    if collection_errors:
        reasons.append("pytest collection failed")
    return "; ".join(_unique_sorted(reasons)) or None


def _empty_report(
    project_path: str | Path,
    *,
    exit_code: int | None = None,
    duration_seconds: float = 0.0,
    report_error: str | None = None,
    timed_out: bool = False,
    incomplete: bool = False,
    failure_reason: str | None = None,
    collection_errors: list[str] | None = None,
) -> RegressionReport:
    return RegressionReport(
        project_path=str(Path(project_path).expanduser().resolve()),
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        report_error=report_error,
        timed_out=timed_out,
        incomplete=incomplete,
        failure_reason=failure_reason,
        collection_errors=_unique_sorted(collection_errors or []),
    )


def parse_regression_report(
    payload: Any,
    project_path: str | Path,
    *,
    exit_code: int | None = None,
    duration_seconds: float = 0.0,
) -> RegressionReport:
    """Parse pytest-json-report data into stable, JSON-serializable evidence.

    Malformed and structurally incomplete reports become an inconclusive
    ``RegressionReport`` rather than raising.  This is important for candidate
    verification: an absent result must never be interpreted as a passing test.
    """
    root = Path(project_path).expanduser().resolve()
    if not isinstance(payload, Mapping):
        error = "pytest report is malformed: expected an object"
        return _empty_report(
            root,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            report_error=error,
            incomplete=True,
            failure_reason=error,
        )

    incomplete_reasons: list[str] = []
    raw_tests = payload.get("tests")
    if not isinstance(raw_tests, list):
        incomplete_reasons.append("tests is missing or not a list")
        raw_tests = []

    tests: list[RegressionTestResult] = []
    seen: set[str] = set()
    for raw_test in raw_tests:
        if not isinstance(raw_test, Mapping):
            incomplete_reasons.append("a test entry is not an object")
            continue
        nodeid = _normalize_nodeid(raw_test.get("nodeid"), root)
        outcome = _phase_outcome(raw_test)
        if not nodeid:
            incomplete_reasons.append("a test entry has no nodeid")
            continue
        if outcome is None:
            incomplete_reasons.append(f"test {nodeid} has no recognized outcome")
            outcome = "unavailable"
        elif (
            not isinstance(raw_test.get("outcome"), str)
            and outcome == "passed"
            and (
                not isinstance(raw_test.get("call"), Mapping)
                or raw_test["call"].get("outcome") not in _KNOWN_OUTCOMES
            )
        ):
            incomplete_reasons.append(f"test {nodeid} has no call outcome")
        if nodeid in seen:
            incomplete_reasons.append(f"duplicate test nodeid: {nodeid}")
            continue
        seen.add(nodeid)
        tests.append(
            RegressionTestResult(
                nodeid=nodeid,
                outcome=outcome,  # type: ignore[arg-type]
                duration_seconds=_test_duration(raw_test),
                message=_phase_message(raw_test),
            )
        )

    tests.sort(key=lambda result: result.nodeid)
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        incomplete_reasons.append("summary is missing or not an object")
        summary_total = None
    else:
        summary_total = _safe_int(summary.get("total"))
        if summary_total is None:
            incomplete_reasons.append("summary.total is missing or not an integer")

    if summary_total is not None and summary_total != len(tests):
        incomplete_reasons.append(
            f"summary.total={summary_total} but parsed {len(tests)} test result(s)"
        )

    collection_errors, collectors_valid = _collection_errors(payload, root)
    if not collectors_valid:
        incomplete_reasons.append("collectors is not a list")

    passed_tests = [result.nodeid for result in tests if result.outcome == "passed"]
    failed_tests = [result.nodeid for result in tests if result.outcome == "failed"]
    skipped_tests = [
        result.nodeid
        for result in tests
        if result.outcome in {"skipped", "xfailed", "xpassed"}
    ]
    unavailable_tests = [
        result.nodeid for result in tests if result.outcome in {"error", "unavailable"}
    ]
    if exit_code == 1 and not failed_tests and not collection_errors:
        incomplete_reasons.append("pytest exited with code 1 without a failed test result")

    return RegressionReport(
        project_path=str(root),
        tests=tests,
        test_count=len(tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
        unavailable_tests=unavailable_tests,
        collection_errors=collection_errors,
        timed_out=False,
        incomplete=bool(incomplete_reasons),
        exit_code=exit_code,
        duration_seconds=max(0.0, duration_seconds),
        failure_reason=_report_failure_reason(
            report_error=None,
            timed_out=False,
            timeout_reason=None,
            incomplete_reasons=incomplete_reasons,
            collection_errors=collection_errors,
        ),
    )


def parse_regression_report_file(
    report_path: str | Path,
    project_path: str | Path,
    *,
    exit_code: int | None = None,
    duration_seconds: float = 0.0,
) -> RegressionReport:
    """Read a pytest-json-report file without allowing malformed input to escape."""
    try:
        raw = Path(report_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError:
        # Pytest uses exit code 5 for a valid empty collection.  A missing
        # report with any other status is an incomplete run.
        if exit_code == 5:
            return _empty_report(project_path, exit_code=exit_code, duration_seconds=duration_seconds)
        error = "pytest report was not produced"
        return _empty_report(
            project_path,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            report_error=error,
            incomplete=True,
            failure_reason=error,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        error = f"pytest report is malformed: {exc.__class__.__name__}"
        return _empty_report(
            project_path,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            report_error=error,
            incomplete=True,
            failure_reason=error,
        )
    return parse_regression_report(
        payload,
        project_path,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
    )


def _last_process_detail(stdout: str, stderr: str) -> str:
    detail = _text(stderr or stdout).strip()
    return detail.splitlines()[-1] if detail else "pytest collection failed"


def _execution_environment(root: Path) -> dict[str, str]:
    """Keep package-style imports working without forwarding obvious secrets."""
    blocked_tokens = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "PRIVATE_KEY")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(token in key.upper() for token in blocked_tokens)
    }
    paths = [str(root), str(root.parent)]
    existing = environment.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def run_regression_tests(
    project_path: str | Path,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> RegressionReport:
    """Run the complete test suite in ``project_path`` and capture its report.

    The function only reads the project.  Callers are responsible for creating
    a disposable candidate copy before passing it here; ``run_candidate`` and
    ``verify_candidate`` make that boundary explicit for main integration.
    """
    root = Path(project_path).expanduser().resolve()
    started = time.monotonic()
    if not root.is_dir():
        error = "project path is not a directory"
        return _empty_report(
            root,
            exit_code=2,
            duration_seconds=time.monotonic() - started,
            collection_errors=[error],
            failure_reason="pytest collection failed: " + error,
        )

    executable = str(python_executable or sys.executable)
    with tempfile.TemporaryDirectory(prefix="faultline-regression-") as temporary:
        report_path = Path(temporary) / "pytest-report.json"
        command = [executable, "-m", "pytest", "--json-report", f"--json-report-file={report_path}", "."]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_execution_environment(root),
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - started
            reason = f"pytest timed out after {timeout:g} seconds"
            return _empty_report(
                root,
                exit_code=124,
                duration_seconds=duration,
                timed_out=True,
                incomplete=True,
                failure_reason=reason,
            )
        except (FileNotFoundError, OSError) as exc:
            duration = time.monotonic() - started
            reason = f"could not start pytest: {exc}"
            return _empty_report(
                root,
                duration_seconds=duration,
                report_error=reason,
                incomplete=True,
                failure_reason=reason,
            )

        duration = time.monotonic() - started
        report = parse_regression_report_file(
            report_path,
            root,
            exit_code=completed.returncode,
            duration_seconds=duration,
        )
        if completed.returncode == 2 and not report.collection_errors:
            report.collection_errors = [
                f"pytest collection failed: {_last_process_detail(completed.stdout, completed.stderr)}"
            ]
            report.failure_reason = _report_failure_reason(
                report_error=report.report_error,
                timed_out=report.timed_out,
                timeout_reason=None,
                incomplete_reasons=[],
                collection_errors=report.collection_errors,
            )
        elif completed.returncode not in {0, 1, 2, 5}:
            report.incomplete = True
            report.failure_reason = (
                report.failure_reason + "; " if report.failure_reason else ""
            ) + f"pytest exited with unexpected code {completed.returncode}"
        elif (
            completed.returncode == 1
            and not report.failed_tests
            and not report.collection_errors
            and not report.incomplete
        ):
            report.incomplete = True
            report.failure_reason = (
                report.failure_reason + "; " if report.failure_reason else ""
            ) + "pytest exited with code 1 without a failed test result"
        return report


def compare_regression_reports(
    baseline: RegressionReport,
    candidate: RegressionReport,
    *,
    duration_seconds: float | None = None,
) -> VerificationReport:
    """Compare stable node outcomes and conservatively classify the result."""
    baseline_outcomes = baseline.outcomes
    candidate_outcomes = candidate.outcomes
    baseline_ids = set(baseline_outcomes)
    candidate_ids = set(candidate_outcomes)

    baseline_failed = _unique_sorted(
        [nodeid for nodeid, outcome in baseline_outcomes.items() if outcome == "failed"]
    )
    candidate_failed = _unique_sorted(
        [nodeid for nodeid, outcome in candidate_outcomes.items() if outcome == "failed"]
    )
    missing = _unique_sorted(list(baseline_ids - candidate_ids))
    added = _unique_sorted(list(candidate_ids - baseline_ids))
    resolved = _unique_sorted(
        [
            nodeid
            for nodeid in baseline_failed
            if candidate_outcomes.get(nodeid) == "passed"
        ]
    )
    unresolved = _unique_sorted(
        [
            nodeid
            for nodeid in baseline_failed
            if nodeid in candidate_outcomes and candidate_outcomes[nodeid] != "passed"
        ]
    )

    # Any candidate outcome other than an ordinary pass is non-passing.  For a
    # previously passing node (or a newly added node), expose it as newly
    # failing; an original failure is instead exposed as unresolved.
    non_passing = _unique_sorted(
        [nodeid for nodeid, outcome in candidate_outcomes.items() if outcome != "passed"]
    )
    newly_failing = _unique_sorted(
        [
            nodeid
            for nodeid in non_passing
            if nodeid not in baseline_outcomes or baseline_outcomes[nodeid] == "passed"
        ]
    )

    baseline_collection_errors = _unique_sorted(list(baseline.collection_errors))
    candidate_collection_errors = _unique_sorted(list(candidate.collection_errors))
    collection_errors = _unique_sorted(baseline_collection_errors + candidate_collection_errors)
    baseline_inconclusive = _unique_sorted(
        [
            nodeid
            for nodeid, outcome in baseline_outcomes.items()
            if outcome in {"skipped", "xfailed", "xpassed", "error", "unavailable"}
        ]
    )
    candidate_inconclusive = _unique_sorted(
        [
            nodeid
            for nodeid, outcome in candidate_outcomes.items()
            if outcome in {"skipped", "xfailed", "xpassed", "error", "unavailable"}
        ]
    )
    timed_out = baseline.timed_out or candidate.timed_out
    incomplete = baseline.incomplete or candidate.incomplete
    baseline_unexpected_exit = baseline.exit_code not in {None, 0, 1, 5}
    candidate_unexpected_exit = candidate.exit_code not in {None, 0, 1, 5}

    reasons: list[str] = []
    if baseline.timed_out:
        reasons.append("baseline timed out")
    if candidate.timed_out:
        reasons.append("candidate timed out")
    if baseline_unexpected_exit:
        reasons.append(f"baseline exited with code {baseline.exit_code}")
    if candidate_unexpected_exit:
        reasons.append(f"candidate exited with code {candidate.exit_code}")
    if baseline.incomplete or baseline.report_error:
        reasons.append("baseline report is incomplete or malformed")
    if candidate.incomplete or candidate.report_error:
        reasons.append("candidate report is incomplete or malformed")
    if collection_errors:
        reasons.append("collection errors were reported")
    if baseline_inconclusive:
        reasons.append("baseline contains skipped or unavailable tests")
    if candidate_inconclusive:
        reasons.append("candidate contains skipped or unavailable tests")
    if missing:
        reasons.append("missing candidate results: " + ", ".join(missing))
    if newly_failing:
        reasons.append("newly failing tests: " + ", ".join(newly_failing))
    # A baseline failure that remains failed is an unresolved pre-existing
    # defect, not a regression introduced by the candidate.  Keep it visible
    # in ``unresolved``/``non_passing_tests`` but do not fail the regression
    # verdict for it.  Only newly failing nodes and changed/inconclusive
    # outcomes block regression verification.
    changed_non_passing = _unique_sorted(
        [
            nodeid
            for nodeid in non_passing
            if nodeid not in baseline_failed
        ]
    )
    if changed_non_passing and not (candidate_inconclusive or newly_failing):
        reasons.append("candidate contains non-passing tests: " + ", ".join(changed_non_passing))

    # Every candidate node must be an ordinary pass, and every baseline node
    # must have a corresponding result.  This makes removed, skipped,
    # unavailable, timeout, malformed, and collection-error states fail closed.
    regression_passed = (
        not baseline.timed_out
        and not candidate.timed_out
        and not baseline_unexpected_exit
        and not candidate_unexpected_exit
        and not baseline.incomplete
        and not candidate.incomplete
        and not baseline.report_error
        and not candidate.report_error
        and not collection_errors
        and not baseline_inconclusive
        and not candidate_inconclusive
        and not missing
        and not newly_failing
        and not changed_non_passing
    )

    return VerificationReport(
        baseline=baseline,
        candidate=candidate,
        baseline_count=baseline.test_count or len(baseline_outcomes),
        candidate_count=candidate.test_count or len(candidate_outcomes),
        failed_tests=candidate_failed,
        baseline_failed_tests=baseline_failed,
        candidate_failed_tests=candidate_failed,
        non_passing_tests=non_passing,
        newly_failing=newly_failing,
        resolved=resolved,
        unresolved=unresolved,
        missing=missing,
        added=added,
        collection_errors=collection_errors,
        baseline_collection_errors=baseline_collection_errors,
        candidate_collection_errors=candidate_collection_errors,
        timed_out=timed_out,
        baseline_timed_out=baseline.timed_out,
        candidate_timed_out=candidate.timed_out,
        incomplete=incomplete,
        baseline_incomplete=baseline.incomplete,
        candidate_incomplete=candidate.incomplete,
        regression_passed=regression_passed,
        duration_seconds=(
            max(0.0, duration_seconds)
            if duration_seconds is not None
            else max(0.0, baseline.duration_seconds + candidate.duration_seconds)
        ),
        failure_reason="; ".join(_unique_sorted(reasons)) or None,
    )


def capture_baseline(
    project_path: str | Path,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> RegressionReport:
    """Capture outcomes from the unmodified project before candidate testing."""
    return run_regression_tests(
        project_path,
        timeout=timeout,
        python_executable=python_executable,
    )


def run_candidate(
    candidate_project_path: str | Path,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> RegressionReport:
    """Run all tests in a caller-provided disposable candidate project copy."""
    return run_regression_tests(
        candidate_project_path,
        timeout=timeout,
        python_executable=python_executable,
    )


def verify_candidate(
    baseline: RegressionReport | str | Path,
    candidate_project_path: str | Path | RegressionReport,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> VerificationReport:
    """Run or compare a disposable candidate against a captured baseline.

    The normal call is ``verify_candidate(baseline_report, candidate_path)``.
    Passing a second ``RegressionReport`` is also supported for integrations
    that already ran the candidate subprocess and only need classification.
    """
    if isinstance(baseline, (str, Path)) and isinstance(candidate_project_path, RegressionReport):
        baseline, candidate_project_path = candidate_project_path, baseline
    if isinstance(candidate_project_path, RegressionReport):
        if not isinstance(baseline, RegressionReport):
            raise TypeError("baseline must be a RegressionReport when candidate is already captured")
        return compare_regression_reports(baseline, candidate_project_path)
    if not isinstance(baseline, RegressionReport):
        raise TypeError("baseline must be a RegressionReport")
    started = time.monotonic()
    candidate = run_candidate(
        candidate_project_path,
        timeout=timeout,
        python_executable=python_executable,
    )
    return compare_regression_reports(
        baseline,
        candidate,
        duration_seconds=time.monotonic() - started + baseline.duration_seconds,
    )


def verify_regression(
    baseline_project_path: str | Path,
    candidate_project_path: str | Path,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> VerificationReport:
    """Capture an unmodified baseline, run a candidate, and compare both."""
    started = time.monotonic()
    baseline = capture_baseline(
        baseline_project_path,
        timeout=timeout,
        python_executable=python_executable,
    )
    candidate = run_candidate(
        candidate_project_path,
        timeout=timeout,
        python_executable=python_executable,
    )
    return compare_regression_reports(
        baseline,
        candidate,
        duration_seconds=time.monotonic() - started,
    )


def verify_candidate_project(
    candidate_project_path: str | Path,
    baseline: RegressionReport,
    *,
    timeout: float = 60.0,
    python_executable: str | Path | None = None,
) -> VerificationReport:
    """Path-first spelling of :func:`verify_candidate` for integration code."""
    return verify_candidate(
        baseline,
        candidate_project_path,
        timeout=timeout,
        python_executable=python_executable,
    )


# Descriptive aliases keep the integration seam discoverable without forcing
# Day 1-3 callers to import this module or change their existing APIs.
capture_test_baseline = capture_baseline
capture_regression_baseline = capture_baseline
capture_candidate = run_candidate
run_pytest_report = run_regression_tests
run_full_suite = run_regression_tests
run_full_test_suite = run_regression_tests
collect_regression = run_regression_tests
capture_baseline_report = capture_baseline
run_candidate_tests = run_candidate
parse_report = parse_regression_report
parse_report_file = parse_regression_report_file
compare_reports = compare_regression_reports
compare_test_reports = compare_regression_reports
verify_candidate_regression = verify_candidate
verify_project = verify_regression
verify = verify_regression
