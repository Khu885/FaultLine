from faultline.arbiter import decide_acceptance
from faultline.models import (
    PatchValidationResult,
    RepairState,
    TestExecutionResult as ExecutionEvidence,
    TestIntegrityResult as IntegrityEvidence,
)
from faultline.regression import compare_regression_reports, parse_regression_report


def _report(tmp_path, name, outcomes, exit_code):
    payload = {
        "summary": {"total": len(outcomes)},
        "tests": [{"nodeid": nodeid, "outcome": outcome} for nodeid, outcome in outcomes.items()],
        "collectors": [],
    }
    return parse_regression_report(payload, tmp_path / name, exit_code=exit_code)


def _evidence(*, targeted=None, integrity=None, verification=None):
    return dict(
        patch_validation=PatchValidationResult(valid=True, changed_paths=["app.py"]),
        targeted_test=targeted
        or ExecutionEvidence(test_node_id="test_app.py::test_value", exit_code=0, passed=True),
        integrity=integrity or IntegrityEvidence(passed=True),
        verification=verification,
    )


def test_accepts_targeted_fix_with_unresolved_preexisting_failure(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"test_app.py::test_value": "failed", "test_app.py::test_unrelated": "failed"},
        1,
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"test_app.py::test_value": "passed", "test_app.py::test_unrelated": "failed"},
        1,
    )
    verification = compare_regression_reports(baseline, candidate)

    decision = decide_acceptance(**_evidence(verification=verification))

    assert decision.status is RepairState.ACCEPTED
    assert decision.accepted
    assert decision.evidence_complete
    assert verification.unresolved == ["test_app.py::test_unrelated"]


def test_rejects_targeted_fix_that_introduces_new_global_failure(tmp_path):
    baseline = _report(
        tmp_path,
        "baseline",
        {"test_app.py::test_value": "failed", "test_app.py::test_other": "passed"},
        1,
    )
    candidate = _report(
        tmp_path,
        "candidate",
        {"test_app.py::test_value": "passed", "test_app.py::test_other": "failed"},
        1,
    )
    verification = compare_regression_reports(baseline, candidate)

    decision = decide_acceptance(**_evidence(verification=verification))

    assert decision.status is RepairState.REJECTED
    assert not decision.accepted
    assert decision.evidence_complete
    assert decision.failed_gates == ["global_regression"]
    assert verification.newly_failing == ["test_app.py::test_other"]


def test_missing_global_evidence_is_inconclusive():
    decision = decide_acceptance(**_evidence())

    assert decision.status is RepairState.INCONCLUSIVE
    assert not decision.accepted
    assert not decision.evidence_complete
    assert decision.failed_gates == ["global_regression"]


def test_timeout_and_collection_failure_are_inconclusive(tmp_path):
    timeout = decide_acceptance(
        **_evidence(
            targeted=ExecutionEvidence(
                test_node_id="test_app.py::test_value",
                exit_code=124,
                timed_out=True,
            )
        )
    )
    baseline = _report(tmp_path, "baseline", {"test_app.py::test_value": "failed"}, 1)
    candidate = _report(tmp_path, "candidate", {}, 2)
    candidate.collection_errors = ["pytest collection failed: SyntaxError"]
    verification = compare_regression_reports(baseline, candidate)
    collection = decide_acceptance(**_evidence(verification=verification))

    assert timeout.status is RepairState.INCONCLUSIVE
    assert collection.status is RepairState.INCONCLUSIVE
    assert not collection.evidence_complete


def test_integrity_violation_rejects_even_when_tests_pass(tmp_path):
    baseline = _report(tmp_path, "baseline", {"test_app.py::test_value": "failed"}, 1)
    candidate = _report(tmp_path, "candidate", {"test_app.py::test_value": "passed"}, 0)
    verification = compare_regression_reports(baseline, candidate)
    decision = decide_acceptance(
        **_evidence(
            verification=verification,
            integrity=IntegrityEvidence(
                passed=False,
                changed_paths=["test_app.py"],
                reason="test integrity violation (changed: test_app.py)",
            ),
        )
    )

    assert decision.status is RepairState.REJECTED
    assert decision.failed_gates == ["test_integrity"]
