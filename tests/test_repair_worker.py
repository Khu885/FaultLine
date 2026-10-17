from pathlib import Path

from faultline.agent_adapter import FixtureAgentAdapter, UnavailableAgentAdapter
from faultline.models import ExecutionMode, FailureRecord, PatchProposal, RepairState, SourceLocation, WorkUnit
from faultline.runner import run_targeted_test
from faultline.worker import RepairWorker


def _write_project(tmp_path: Path, body: str, test_body: str) -> WorkUnit:
    (tmp_path / "app.py").write_text(body, encoding="utf-8")
    (tmp_path / "test_app.py").write_text(test_body, encoding="utf-8")
    return WorkUnit(
        work_unit_id="unit-1",
        source_file=str(tmp_path / "app.py"),
        target_function="value",
        test_node_ids=["test_app.py::test_value"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_value",
                traceback="traceback",
                message="AssertionError",
                source_location=SourceLocation(status="resolved", path=str(tmp_path / "app.py"), line=2, function="value"),
            )
        ],
    )


def _proposal(diff: str, attempt: int = 1) -> PatchProposal:
    return PatchProposal(
        work_unit_id="unit-1",
        target_file="app.py",
        root_cause_explanation="The implementation returns the wrong value.",
        unified_diff=diff,
        attempt_number=attempt,
        execution_mode=ExecutionMode.FIXTURE,
    )


VALID_DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"
INVALID_DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return (\n"


def test_worker_accepts_only_runtime_verified_patch_and_preserves_original(tmp_path):
    work_unit = _write_project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    report = RepairWorker(FixtureAgentAdapter([_proposal(VALID_DIFF)])).repair(tmp_path, work_unit)

    assert report.final_status is RepairState.ACCEPTED
    assert report.targeted_test_passed
    assert report.test_integrity_passed
    assert report.acceptance_decision is not None
    assert report.acceptance_decision.accepted
    assert report.verification_report is not None
    assert report.verification_report.regression_passed
    assert (tmp_path / "app.py").read_text(encoding="utf-8").endswith("return 1\n")


def test_worker_retries_from_a_fresh_copy_and_records_feedback(tmp_path):
    work_unit = _write_project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    agent = FixtureAgentAdapter([_proposal(INVALID_DIFF), _proposal(VALID_DIFF, attempt=2)])

    report = RepairWorker(agent, max_attempts=2).repair(tmp_path, work_unit)

    assert report.final_status is RepairState.ACCEPTED
    assert len(report.attempts) == 2
    assert report.attempts[0].failure_reason == "syntax_error"
    assert report.attempts[0].state_history[-1] is RepairState.RETRYING
    assert agent.requests[1].previous_feedback


def test_worker_reports_agent_unavailable_without_fake_fallback(tmp_path):
    work_unit = _write_project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )

    report = RepairWorker(UnavailableAgentAdapter()).repair(tmp_path, work_unit)

    assert report.final_status is RepairState.FAILED
    assert report.execution_mode is ExecutionMode.UNAVAILABLE
    assert report.attempts[0].failure_reason == "agent_unavailable"


def test_worker_rejects_runtime_test_integrity_violation(tmp_path):
    work_unit = _write_project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    malicious = _proposal(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return (__import__('pathlib').Path('test_app.py').write_text('changed\\n') or 2)\n"
    )

    report = RepairWorker(FixtureAgentAdapter([malicious]), max_attempts=1).repair(tmp_path, work_unit)

    assert report.final_status is RepairState.FAILED
    assert report.attempts[0].failure_reason == "test_integrity_violation"
    assert not report.test_integrity_passed
    assert report.attempts[0].integrity_result is not None


def test_worker_reports_patch_validation_rejection_as_rejected(tmp_path):
    work_unit = _write_project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    malformed = _proposal("not a unified diff")

    report = RepairWorker(FixtureAgentAdapter([malformed]), max_attempts=1).repair(tmp_path, work_unit)

    assert report.final_status is RepairState.REJECTED
    assert report.attempts[0].failure_reason == "patch_rejected"


def test_runner_reports_timeout_and_missing_executable(tmp_path):
    (tmp_path / "test_sleep.py").write_text(
        "import time\n\ndef test_sleep():\n    time.sleep(1)\n", encoding="utf-8"
    )
    timeout = run_targeted_test(tmp_path, "test_sleep.py::test_sleep", timeout=0.05)
    missing = run_targeted_test(tmp_path, "test_sleep.py::test_sleep", python_executable="does-not-exist")

    assert timeout.timed_out
    assert timeout.exit_code == 124
    assert not missing.passed
    assert "could not start pytest" in missing.stderr
