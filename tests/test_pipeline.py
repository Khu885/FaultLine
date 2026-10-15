from faultline.collector import collect_project, parse_report_file
from faultline.models import FailureRecord, SourceLocation
from faultline.planner import group_work_units


def test_failed_tests_are_distinct_from_collection_errors(tmp_path):
    (tmp_path / "test_failure.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")

    report = collect_project(tmp_path)

    assert report.failures
    assert report.collection_error is None
    assert report.exit_code == 1


def test_collection_errors_are_reported_without_raising(tmp_path):
    (tmp_path / "test_broken.py").write_text("def test_broken(:\n    pass\n", encoding="utf-8")

    report = collect_project(tmp_path)

    assert report.collection_error
    assert not report.failures
    assert report.exit_code == 2


def test_missing_and_malformed_reports_are_structured(tmp_path):
    missing = parse_report_file(tmp_path / "missing.json", tmp_path)
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("not-json", encoding="utf-8")
    malformed = parse_report_file(malformed_path, tmp_path)

    assert missing.report_error == "pytest report was not produced"
    assert malformed.report_error == "pytest report is malformed: JSONDecodeError"


def test_failures_group_only_when_source_file_is_known():
    first = FailureRecord(nodeid="a", source_location=SourceLocation(status="resolved", path="app.py", line=2))
    second = FailureRecord(nodeid="b", source_location=SourceLocation(status="resolved", path="app.py", line=8))
    unresolved = FailureRecord(nodeid="c")

    units = group_work_units([first, second, unresolved])

    assert [unit.source_file for unit in units] == ["app.py", None]
    assert [unit.failure_count for unit in units] == [2, 1]
