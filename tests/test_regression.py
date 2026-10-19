import json
import shutil
import subprocess

from faultline.models import RegressionReport, RegressionTestResult, VerificationReport
from faultline.regression import (
    compare_regression_reports,
    parse_regression_report,
    parse_regression_report_file,
    run_regression_tests,
    verify_regression,
)


def _payload(outcomes: dict[str, str], *, collectors=None, total=None) -> dict:
    return {
        "summary": {"total": len(outcomes) if total is None else total},
        "tests": [{"nodeid": nodeid, "outcome": outcome} for nodeid, outcome in outcomes.items()],
        "collectors": [] if collectors is None else collectors,
    }


def _report(tmp_path, name: str, outcomes: dict[str, str], **kwargs) -> RegressionReport:
    return parse_regression_report(_payload(outcomes, **kwargs), tmp_path / name)


def test_resolved_original_failure_is_an_improvement_and_can_pass(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"tests/test_app.py::test_original": "failed", "tests/test_app.py::test_ok": "passed"},
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"tests/test_app.py::test_original": "passed", "tests/test_app.py::test_ok": "passed"},
    )

    result = compare_regression_reports(baseline, candidate)

    assert result.regression_passed
    assert result.resolved == ["tests/test_app.py::test_original"]
    assert result.unresolved == []
    assert result.newly_failing == []
    assert result.failure_reason is None


def test_unrelated_original_failure_remaining_is_unresolved_but_not_a_regression(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"tests/test_app.py::test_original": "failed", "tests/test_app.py::test_unrelated": "failed"},
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"tests/test_app.py::test_original": "passed", "tests/test_app.py::test_unrelated": "failed"},
    )

    result = compare_regression_reports(baseline, candidate)

    assert result.regression_passed
    assert result.resolved == ["tests/test_app.py::test_original"]
    assert result.unresolved == ["tests/test_app.py::test_unrelated"]
    assert result.candidate_failed_tests == ["tests/test_app.py::test_unrelated"]
    assert result.failure_reason is None


def test_previously_passing_test_becoming_failed_is_new_regression(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"tests/test_app.py::test_original": "failed", "tests/test_app.py::test_passing": "passed"},
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"tests/test_app.py::test_original": "passed", "tests/test_app.py::test_passing": "failed"},
    )

    result = compare_regression_reports(baseline, candidate)

    assert not result.regression_passed
    assert result.resolved == ["tests/test_app.py::test_original"]
    assert result.newly_failing == ["tests/test_app.py::test_passing"]
    assert result.unresolved == []


def test_missing_and_skipped_results_fail_closed(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"tests/test_app.py::test_missing": "passed", "tests/test_app.py::test_skipped": "passed"},
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"tests/test_app.py::test_skipped": "skipped"},
    )

    result = compare_regression_reports(baseline, candidate)

    assert not result.regression_passed
    assert result.missing == ["tests/test_app.py::test_missing"]
    assert result.newly_failing == ["tests/test_app.py::test_skipped"]
    assert result.candidate.skipped_tests == ["tests/test_app.py::test_skipped"]
    assert "missing candidate results" in result.failure_reason


def test_collection_errors_are_reported_as_inconclusive(tmp_path):
    baseline = _report(tmp_path, "baseline", {"tests/test_app.py::test_ok": "passed"})
    candidate = _report(
        tmp_path,
        "candidate",
        {},
        collectors=[{"nodeid": "tests/test_broken.py", "outcome": "failed", "longrepr": "SyntaxError"}],
    )

    result = compare_regression_reports(baseline, candidate)

    assert not result.regression_passed
    assert result.candidate_collection_errors
    assert "pytest collection failed" in result.candidate_collection_errors[0]
    assert result.collection_errors == result.candidate_collection_errors
    assert "collection errors" in result.failure_reason


def test_timeout_is_incomplete_and_never_passes(tmp_path, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["pytest"], 0.01)

    monkeypatch.setattr("faultline.regression.subprocess.run", timeout)

    report = run_regression_tests(tmp_path, timeout=0.01)
    candidate = _report(tmp_path, "candidate", {})
    result = compare_regression_reports(report, candidate)

    assert report.timed_out
    assert report.incomplete
    assert "timed out" in report.failure_reason
    assert not result.regression_passed
    assert result.baseline_timed_out


def test_malformed_and_incomplete_reports_are_structured(tmp_path):
    missing = parse_regression_report_file(tmp_path / "missing.json", tmp_path / "project")
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("not-json", encoding="utf-8")
    malformed = parse_regression_report_file(malformed_path, tmp_path / "project")
    incomplete = parse_regression_report({"tests": []}, tmp_path / "project")

    assert missing.incomplete
    assert missing.report_error == "pytest report was not produced"
    assert malformed.incomplete
    assert malformed.report_error == "pytest report is malformed: JSONDecodeError"
    assert incomplete.incomplete
    assert "summary" in incomplete.failure_reason


def test_identical_baseline_and_candidate_reports_pass(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"tests/test_app.py::test_a": "passed", "tests/test_app.py::test_b": "passed"},
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"tests/test_app.py::test_a": "passed", "tests/test_app.py::test_b": "passed"},
    )

    result = compare_regression_reports(baseline, candidate)

    assert result.regression_passed
    assert result.baseline_count == result.candidate_count == 2
    assert result.failed_tests == []
    assert result.resolved == result.unresolved == result.missing == result.added == []


def test_project_name_wrapped_nodeids_normalize_for_disposable_copies(tmp_path):
    project = tmp_path / "benchmarks"
    project.mkdir()
    (project / "test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")

    report = parse_regression_report(
        _payload({"benchmarks/test_app.py::test_ok": "passed"}),
        project,
    )

    assert report.outcomes == {"test_app.py::test_ok": "passed"}


def test_added_passing_test_is_reported_without_being_a_regression(tmp_path):
    baseline = _report(tmp_path, "baseline", {"tests/test_app.py::test_existing": "passed"})
    candidate = _report(
        tmp_path,
        "candidate",
        {
            "tests/test_app.py::test_existing": "passed",
            "tests/test_app.py::test_added": "passed",
        },
    )

    result = compare_regression_reports(baseline, candidate)

    assert result.regression_passed
    assert result.added == ["tests/test_app.py::test_added"]


def test_verify_regression_captures_unmodified_baseline_and_runs_candidate(tmp_path):
    baseline_path = tmp_path / "baseline"
    candidate_path = tmp_path / "candidate"
    baseline_path.mkdir()
    (baseline_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (baseline_path / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    shutil.copytree(baseline_path, candidate_path)
    (candidate_path / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")

    result = verify_regression(baseline_path, candidate_path)

    assert result.baseline_count == result.candidate_count == 1
    assert result.resolved == ["test_app.py::test_value"]
    assert result.regression_passed
    assert (baseline_path / "app.py").read_text(encoding="utf-8").endswith("return 1\n")


def test_regression_models_are_json_serializable(tmp_path):
    baseline = _report(tmp_path, "baseline", {"tests/test_app.py::test_ok": "passed"})
    candidate = _report(tmp_path, "candidate", {"tests/test_app.py::test_ok": "passed"})
    result = compare_regression_reports(baseline, candidate)

    encoded = json.loads(result.model_dump_json())

    assert encoded["baseline"]["tests"][0]["nodeid"] == "tests/test_app.py::test_ok"
    assert encoded["candidate_count"] == 1
    assert encoded["regression_passed"] is True
    assert RegressionTestResult.model_validate(encoded["baseline"]["tests"][0]).passed

    schema = VerificationReport.model_json_schema()
    assert "baseline" in schema["properties"]
    assert "regression_passed" in schema["properties"]
    assert "failure_reason" in schema["properties"]
