import shutil
from pathlib import Path

from faultline.agent_adapter import FixtureAgentAdapter
from faultline.models import (
    ExecutionMode,
    FailureRecord,
    PatchProposal,
    RepairState,
    RegressionReport,
    SourceLocation,
    WorkUnit,
)
from faultline.worker import RepairWorker


def _pagination_unit(project: Path) -> WorkUnit:
    return WorkUnit(
        work_unit_id="pagination",
        source_file=str(project / "off_by_one_pagination.py"),
        target_function="paginate",
        test_node_ids=["test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page"],
        failures=[
            FailureRecord(
                nodeid="test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page",
                traceback="AssertionError",
                message="the final page item is missing",
                source_location=SourceLocation(
                    status="resolved",
                    path=str(project / "off_by_one_pagination.py"),
                    line=8,
                    function="paginate",
                ),
            )
        ],
    )


def _proposal(unit_id: str, target_file: str, diff: str) -> PatchProposal:
    return PatchProposal(
        work_unit_id=unit_id,
        target_file=target_file,
        root_cause_explanation="The inclusive page endpoint was omitted from the slice.",
        unified_diff=diff,
        attempt_number=1,
        execution_mode=ExecutionMode.FIXTURE,
    )


def test_worker_accepts_real_pagination_repair_after_global_regression_check(tmp_path):
    source_root = Path(__file__).parents[1] / "benchmarks"
    project = tmp_path / "benchmarks"
    project.mkdir()
    shutil.copy2(source_root / "off_by_one_pagination.py", project / "off_by_one_pagination.py")
    shutil.copy2(source_root / "test_off_by_one_pagination.py", project / "test_off_by_one_pagination.py")
    unit = _pagination_unit(project)
    proposal = _proposal(
        "pagination",
        "off_by_one_pagination.py",
        "--- a/off_by_one_pagination.py\n"
        "+++ b/off_by_one_pagination.py\n"
        "@@ -6,3 +6,3 @@\n"
        "     start = page * page_size\n"
        "-    stop = start + page_size - 1\n"
        "+    stop = start + page_size\n"
        "     return items[start:stop]\n",
    )

    report = RepairWorker(FixtureAgentAdapter([proposal]), max_attempts=1).repair(project, unit)

    assert report.final_status is RepairState.ACCEPTED
    assert report.acceptance_decision is not None
    assert report.acceptance_decision.accepted
    assert report.verification_report is not None
    assert report.verification_report.resolved == [
        "test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page"
    ]
    assert (project / "off_by_one_pagination.py").read_text(encoding="utf-8").endswith(
        "return items[start:stop]\n"
    )


def test_worker_rejects_targeted_pass_when_global_regression_is_new(tmp_path):
    (tmp_path / "app.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\n"
        "def test_target():\n    assert value() == 2\n\n"
        "def test_previously_passing():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    unit = WorkUnit(
        work_unit_id="new-regression",
        source_file=str(tmp_path / "app.py"),
        target_function="value",
        test_node_ids=["test_app.py::test_target"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_target",
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved", path=str(tmp_path / "app.py"), line=2, function="value"
                ),
            )
        ],
    )
    proposal = _proposal(
        "new-regression",
        "app.py",
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    )

    report = RepairWorker(FixtureAgentAdapter([proposal]), max_attempts=1).repair(tmp_path, unit)

    assert report.final_status is RepairState.REJECTED
    assert report.targeted_test_passed
    assert report.test_integrity_passed
    assert report.acceptance_decision is not None
    assert report.acceptance_decision.failed_gates == ["global_regression"]
    assert report.verification_report is not None
    assert report.verification_report.newly_failing == ["test_app.py::test_previously_passing"]


def test_worker_reports_inconclusive_when_baseline_times_out(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    unit = WorkUnit(
        work_unit_id="inconclusive",
        source_file=str(tmp_path / "app.py"),
        target_function="value",
        test_node_ids=["test_app.py::test_value"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_value",
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved", path=str(tmp_path / "app.py"), line=2, function="value"
                ),
            )
        ],
    )
    proposal = _proposal(
        "inconclusive",
        "app.py",
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    )

    def timed_out_baseline(project_path, **_kwargs):
        return RegressionReport(
            project_path=str(project_path),
            timed_out=True,
            incomplete=True,
            exit_code=124,
            failure_reason="pytest timed out after 0.01 seconds",
        )

    monkeypatch.setattr("faultline.worker.capture_baseline", timed_out_baseline)
    report = RepairWorker(FixtureAgentAdapter([proposal]), max_attempts=1).repair(tmp_path, unit)

    assert report.final_status is RepairState.INCONCLUSIVE
    assert report.targeted_test_passed
    assert report.acceptance_decision is not None
    assert not report.acceptance_decision.evidence_complete
    assert report.acceptance_decision.status is RepairState.INCONCLUSIVE
