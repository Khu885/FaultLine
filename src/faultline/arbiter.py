"""Fail-closed final acceptance decisions for one candidate patch."""

from __future__ import annotations

from .models import (
    AcceptanceDecision,
    PatchValidationResult,
    RepairState,
    TestExecutionResult,
    TestIntegrityResult,
    VerificationReport,
)


def _decision(
    status: RepairState,
    reason: str,
    *,
    evidence_complete: bool,
    failed_gates: list[str],
) -> AcceptanceDecision:
    return AcceptanceDecision(
        status=status,
        accepted=status is RepairState.ACCEPTED,
        evidence_complete=evidence_complete,
        failed_gates=failed_gates,
        reason=reason,
    )


def _verification_is_inconclusive(report: VerificationReport) -> bool:
    """Identify unavailable evidence separately from a confirmed regression."""
    return (
        report.has_inconclusive_results
        or report.baseline.report_error is not None
        or report.candidate.report_error is not None
        or report.baseline.exit_code is None
        or report.candidate.exit_code is None
    )


def decide_acceptance(
    *,
    patch_validation: PatchValidationResult | None,
    targeted_test: TestExecutionResult | None,
    integrity: TestIntegrityResult | None,
    verification: VerificationReport | None,
    execution_error: str | None = None,
) -> AcceptanceDecision:
    """Apply the mandatory acceptance gates in a deterministic order.

    Invalid or unsafe evidence rejects a candidate.  Missing, timed-out, or
    infrastructure-generated evidence is explicitly inconclusive instead of
    being treated as a pass.  A pre-existing baseline failure is allowed when
    ``VerificationReport.regression_passed`` is true; the regression engine
    already distinguishes it from a newly failing test.
    """
    if execution_error:
        return _decision(
            RepairState.FAILED,
            f"candidate execution failed: {execution_error}",
            evidence_complete=False,
            failed_gates=["candidate_execution"],
        )

    if patch_validation is None:
        return _decision(
            RepairState.INCONCLUSIVE,
            "patch validation evidence is missing",
            evidence_complete=False,
            failed_gates=["patch_validation"],
        )
    if not patch_validation.valid:
        return _decision(
            RepairState.REJECTED,
            patch_validation.reason or "patch validation rejected the candidate",
            evidence_complete=True,
            failed_gates=["patch_validation"],
        )

    if targeted_test is None:
        return _decision(
            RepairState.INCONCLUSIVE,
            "targeted test evidence is missing",
            evidence_complete=False,
            failed_gates=["targeted_test"],
        )
    if targeted_test.timed_out:
        return _decision(
            RepairState.INCONCLUSIVE,
            "targeted test timed out before acceptance could be decided",
            evidence_complete=False,
            failed_gates=["targeted_test"],
        )
    if targeted_test.collection_error or targeted_test.exit_code is None:
        return _decision(
            RepairState.INCONCLUSIVE,
            "targeted test execution did not produce a usable result",
            evidence_complete=False,
            failed_gates=["targeted_test"],
        )
    if not targeted_test.passed:
        return _decision(
            RepairState.REJECTED,
            "targeted test did not pass",
            evidence_complete=True,
            failed_gates=["targeted_test"],
        )

    if integrity is None:
        return _decision(
            RepairState.INCONCLUSIVE,
            "test integrity evidence is missing",
            evidence_complete=False,
            failed_gates=["test_integrity"],
        )
    if not integrity.passed:
        return _decision(
            RepairState.REJECTED,
            integrity.reason or "protected test files changed during candidate execution",
            evidence_complete=True,
            failed_gates=["test_integrity"],
        )

    if verification is None:
        return _decision(
            RepairState.INCONCLUSIVE,
            "global regression evidence is missing",
            evidence_complete=False,
            failed_gates=["global_regression"],
        )
    if _verification_is_inconclusive(verification):
        return _decision(
            RepairState.INCONCLUSIVE,
            verification.failure_reason or "global regression evidence is incomplete",
            evidence_complete=False,
            failed_gates=["global_regression"],
        )
    if not verification.regression_passed:
        return _decision(
            RepairState.REJECTED,
            verification.failure_reason or "candidate introduced a global regression",
            evidence_complete=True,
            failed_gates=["global_regression"],
        )

    return _decision(
        RepairState.ACCEPTED,
        "patch passed validation, targeted testing, test integrity, and global regression gates",
        evidence_complete=True,
        failed_gates=[],
    )


class AcceptanceArbiter:
    """Small object-oriented seam for callers that prefer dependency injection."""

    def decide(self, **kwargs) -> AcceptanceDecision:
        return decide_acceptance(**kwargs)


# Descriptive aliases keep the integration seam easy to discover.
arbitrate = decide_acceptance
evaluate_patch = decide_acceptance
accept_patch = decide_acceptance
