"""Pydantic models shared by FaultLine's deterministic pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LocationStatus = Literal["resolved", "unresolved"]
LocationCategory = Literal["application", "test_assertion", "test", "unresolved"]


class ExecutionMode(StrEnum):
    """How a repair proposal was produced."""

    FIXTURE = "offline_fixture"
    OMNIRUSH_HUMAN_MEDIATED = "omnirush_human_mediated"
    OMNIRUSH_AUTOMATED = "omnirush_automated"
    GROQ_AUTOMATED = "groq_automated"
    UNAVAILABLE = "unavailable"


class RepairState(StrEnum):
    """Observable lifecycle states for one repair worker."""

    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    PATCH_GENERATED = "PATCH_GENERATED"
    VALIDATING = "VALIDATING"
    TESTING = "TESTING"
    RETRYING = "RETRYING"
    PROVISIONALLY_SUCCESSFUL = "PROVISIONALLY_SUCCESSFUL"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


FailureReason = Literal[
    "agent_unavailable",
    "invalid_proposal",
    "patch_rejected",
    "syntax_error",
    "test_assertion_failure",
    "test_timeout",
    "test_collection_failure",
    "test_integrity_violation",
    "global_regression",
    "verification_inconclusive",
    "retry_exhausted",
    "provisional_success",
]


class SourceLocation(BaseModel):
    """The result of mapping a traceback frame to Python source."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: LocationStatus
    path: str | None = None
    line: int | None = None
    function: str | None = None
    reason: str | None = None
    category: LocationCategory = "unresolved"
    is_test: bool = False
    is_assertion: bool = False

    @property
    def file_path(self) -> str | None:
        return self.path

    @property
    def line_number(self) -> int | None:
        return self.line

    @property
    def qualified_name(self) -> str | None:
        return self.function

    @property
    def name(self) -> str | None:
        return self.function.rsplit(".", 1)[-1] if self.function else None

    @property
    def symbol(self) -> str | None:
        return self.function

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def is_test_assertion(self) -> bool:
        return self.category == "test_assertion"


class FailureRecord(BaseModel):
    """One failed test extracted from pytest-json-report."""

    model_config = ConfigDict(extra="ignore")

    nodeid: str
    outcome: Literal["failed"] = "failed"
    message: str = ""
    traceback: str = ""
    source_location: SourceLocation | None = None
    test_name: str | None = None
    phase: str = "call"

    @property
    def source_file(self) -> str | None:
        return self.source_location.path if self.source_location else None


class WorkUnit(BaseModel):
    """A conservative group of failures associated with one source file."""

    model_config = ConfigDict(extra="ignore")

    work_unit_id: str = ""
    source_file: str | None = None
    target_function: str | None = None
    test_node_ids: list[str] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def identifier(self) -> str:
        """Stable identifier, including compatibility for hand-built units."""
        return self.work_unit_id or self.source_file or (self.failures[0].nodeid if self.failures else "empty")

    @property
    def failing_test_node_id(self) -> str | None:
        return self.test_node_ids[0] if self.test_node_ids else (self.failures[0].nodeid if self.failures else None)


class ScanReport(BaseModel):
    """Complete deterministic result of a FaultLine test scan."""

    model_config = ConfigDict(extra="ignore")

    project_path: str
    failures: list[FailureRecord] = Field(default_factory=list)
    work_units: list[WorkUnit] = Field(default_factory=list)
    collection_error: str | None = None
    report_error: str | None = None
    test_count: int = 0
    exit_code: int = 0

    @property
    def test_failures(self) -> list[FailureRecord]:
        return self.failures

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    @property
    def is_empty(self) -> bool:
        return self.test_count == 0 and not self.failures and self.collection_error is None


class RepairRequest(BaseModel):
    """Auditable context sent to a repair agent."""

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    target_file: str
    target_function: str | None = None
    source_snippet: str
    source_line_start: int = Field(default=1, ge=1)
    traceback: str
    failing_test_node_id: str
    exception_class: str | None = None
    error_message: str = ""
    previous_feedback: str | None = None
    authorized_paths: list[str] = Field(default_factory=list)
    repair_constraints: list[str] = Field(default_factory=list)


class PatchProposal(BaseModel):
    """Structured candidate patch returned by an agent."""

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    status: Literal["proposed", "rejected"] = "proposed"
    target_file: str
    root_cause_explanation: str
    unified_diff: str
    attempt_number: int = Field(ge=1)
    execution_mode: ExecutionMode
    diagnostic_notes: str | None = None


class PatchValidationResult(BaseModel):
    """Result of validating and, when requested, applying a patch."""

    valid: bool
    reason: str | None = None
    changed_paths: list[str] = Field(default_factory=list)
    syntax_error: str | None = None


class TestExecutionResult(BaseModel):
    """Captured result of one targeted pytest invocation."""

    test_node_id: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    passed: bool = False
    collection_error: bool = False


class TestIntegritySnapshot(BaseModel):
    """Inventory and SHA-256 hashes for protected test files."""

    files: dict[str, str] = Field(default_factory=dict)


class TestIntegrityResult(BaseModel):
    """Comparison of a protected test inventory before and after execution."""

    passed: bool
    changed_paths: list[str] = Field(default_factory=list)
    added_paths: list[str] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    reason: str | None = None


class MutationOperator(StrEnum):
    """Deterministic AST mutation families supported by FaultLine."""

    COMPARISON_INVERSION = "comparison_inversion"
    ARITHMETIC_REPLACEMENT = "arithmetic_replacement"
    NUMERIC_PERTURBATION = "numeric_perturbation"
    BOOLEAN_INVERSION = "boolean_inversion"
    RETURN_VALUE_REPLACEMENT = "return_value_replacement"


class MutationOutcome(StrEnum):
    """Classification of one independently executed mutant."""

    KILLED = "KILLED"
    SURVIVED = "SURVIVED"
    INCONCLUSIVE = "INCONCLUSIVE"


class MutationResult(BaseModel):
    """Auditable execution and classification evidence for one mutant."""

    model_config = ConfigDict(extra="forbid")

    mutation_id: str
    operator: MutationOperator
    target_file: str
    target_function: str
    source_line: int
    outcome: MutationOutcome
    test_node_ids: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    failure_reason: str | None = None


class MutationReport(BaseModel):
    """Structured mutation-quality evidence for one verified candidate."""

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    total_generated: int = 0
    killed: int = 0
    survived: int = 0
    inconclusive: int = 0
    kill_rate: float | None = None
    results: list[MutationResult] = Field(default_factory=list)
    duration_seconds: float = 0.0
    execution_mode: ExecutionMode
    quality_warning: str | None = None
    limitations: list[str] = Field(default_factory=list)

    @property
    def score_available(self) -> bool:
        """Whether at least one conclusive mutant contributes to the score."""
        return (self.killed + self.survived) > 0


RegressionOutcome = Literal[
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "error",
    "unavailable",
]


class RegressionTestResult(BaseModel):
    """One stable pytest node outcome used by global regression checks."""

    model_config = ConfigDict(extra="ignore")

    nodeid: str
    outcome: RegressionOutcome
    duration_seconds: float = 0.0
    message: str = ""

    @property
    def passed(self) -> bool:
        """Whether pytest reported the node as an ordinary passing test."""
        return self.outcome == "passed"


class RegressionReport(BaseModel):
    """JSON evidence captured from one complete pytest run.

    The report intentionally keeps node outcomes as a list instead of a set or
    opaque pytest object so it can be serialized and compared across a fresh
    candidate copy.  ``tests`` is ordered deterministically by node id by the
    regression runner.
    """

    model_config = ConfigDict(extra="ignore")

    project_path: str
    tests: list[RegressionTestResult] = Field(default_factory=list)
    outcomes: dict[str, RegressionOutcome] = Field(default_factory=dict)
    test_count: int = 0
    passed_tests: list[str] = Field(default_factory=list)
    failed_tests: list[str] = Field(default_factory=list)
    skipped_tests: list[str] = Field(default_factory=list)
    unavailable_tests: list[str] = Field(default_factory=list)
    collection_errors: list[str] = Field(default_factory=list)
    timed_out: bool = False
    incomplete: bool = False
    exit_code: int | None = None
    report_error: str | None = None
    duration_seconds: float = 0.0
    failure_reason: str | None = None

    @model_validator(mode="after")
    def _fill_derived_outcomes(self) -> "RegressionReport":
        """Keep hand-built reports as useful as parser-produced reports."""
        if not self.tests and not self.outcomes:
            self.outcomes = {
                **{nodeid: "passed" for nodeid in self.passed_tests},
                **{nodeid: "failed" for nodeid in self.failed_tests},
                **{nodeid: "skipped" for nodeid in self.skipped_tests},
                **{nodeid: "unavailable" for nodeid in self.unavailable_tests},
            }
        if not self.tests and self.outcomes:
            self.tests = [
                RegressionTestResult(nodeid=nodeid, outcome=outcome)
                for nodeid, outcome in sorted(self.outcomes.items())
            ]
        outcomes = {result.nodeid: result.outcome for result in self.tests}
        self.outcomes = outcomes
        if not self.test_count:
            self.test_count = len(outcomes)
        elif self.test_count != len(outcomes):
            self.incomplete = True
            self.failure_reason = self.failure_reason or (
                f"incomplete test result count: expected {self.test_count}, parsed {len(outcomes)}"
            )
        if not self.passed_tests:
            self.passed_tests = [nodeid for nodeid, outcome in outcomes.items() if outcome == "passed"]
        if not self.failed_tests:
            self.failed_tests = [nodeid for nodeid, outcome in outcomes.items() if outcome == "failed"]
        if not self.skipped_tests:
            self.skipped_tests = [
                nodeid
                for nodeid, outcome in outcomes.items()
                if outcome in {"skipped", "xfailed", "xpassed"}
            ]
        if not self.unavailable_tests:
            self.unavailable_tests = [
                nodeid
                for nodeid, outcome in outcomes.items()
                if outcome in {"error", "unavailable"}
            ]
        return self

    @property
    def test_results(self) -> list[RegressionTestResult]:
        """Compatibility-friendly name for callers that use report results."""
        return self.tests

    @property
    def results(self) -> list[RegressionTestResult]:
        """Short alias for stable per-node results."""
        return self.tests

    @property
    def collection_error(self) -> str | None:
        """Return the first collection error for scalar-oriented callers."""
        return self.collection_errors[0] if self.collection_errors else None

    @property
    def timeout(self) -> bool:
        """Alias for ``timed_out`` used by some integration adapters."""
        return self.timed_out

    @property
    def duration(self) -> float:
        """Alias for the measured run duration in seconds."""
        return self.duration_seconds


class VerificationReport(BaseModel):
    """Comparison of an unmodified baseline and a candidate test run."""

    model_config = ConfigDict(extra="ignore")

    baseline: RegressionReport
    candidate: RegressionReport
    baseline_count: int = 0
    candidate_count: int = 0
    failed_tests: list[str] = Field(default_factory=list)
    baseline_failed_tests: list[str] = Field(default_factory=list)
    candidate_failed_tests: list[str] = Field(default_factory=list)
    non_passing_tests: list[str] = Field(default_factory=list)
    newly_failing: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    collection_errors: list[str] = Field(default_factory=list)
    baseline_collection_errors: list[str] = Field(default_factory=list)
    candidate_collection_errors: list[str] = Field(default_factory=list)
    timed_out: bool = False
    baseline_timed_out: bool = False
    candidate_timed_out: bool = False
    incomplete: bool = False
    baseline_incomplete: bool = False
    candidate_incomplete: bool = False
    regression_passed: bool = False
    duration_seconds: float = 0.0
    failure_reason: str | None = None
    mutation_report: MutationReport | None = None

    @property
    def timeout(self) -> bool:
        """Alias for ``timed_out`` for integrations using timeout terminology."""
        return self.timed_out

    @property
    def duration(self) -> float:
        """Alias for the measured verification duration in seconds."""
        return self.duration_seconds

    @property
    def baseline_report(self) -> RegressionReport:
        """Descriptive alias for the captured baseline report."""
        return self.baseline

    @property
    def candidate_report(self) -> RegressionReport:
        """Descriptive alias for the candidate report."""
        return self.candidate

    @property
    def new_failures(self) -> list[str]:
        """Alias for newly failing stable node ids."""
        return self.newly_failing

    @property
    def missing_tests(self) -> list[str]:
        """Alias for baseline node ids with no candidate result."""
        return self.missing

    @property
    def added_tests(self) -> list[str]:
        """Alias for candidate-only stable node ids."""
        return self.added

    @property
    def collection_error(self) -> str | None:
        """Return the first combined collection error, when present."""
        return self.collection_errors[0] if self.collection_errors else None

    @property
    def has_inconclusive_results(self) -> bool:
        """Whether either side lacks a complete, collection-clean result."""
        inconclusive_outcomes = {"skipped", "xfailed", "xpassed", "error", "unavailable"}
        return (
            self.incomplete
            or bool(self.collection_errors)
            or self.timed_out
            or any(outcome in inconclusive_outcomes for outcome in self.baseline.outcomes.values())
            or any(outcome in inconclusive_outcomes for outcome in self.candidate.outcomes.values())
        )


class AcceptanceDecision(BaseModel):
    """Deterministic final decision from the patch acceptance arbiter."""

    model_config = ConfigDict(extra="forbid")

    status: RepairState
    accepted: bool = False
    evidence_complete: bool = False
    failed_gates: list[str] = Field(default_factory=list)
    reason: str


class RepairAttempt(BaseModel):
    """Evidence from one independent candidate generation attempt."""

    attempt_number: int
    state_history: list[RepairState] = Field(default_factory=list)
    proposal: PatchProposal | None = None
    validation_result: PatchValidationResult | None = None
    test_result: TestExecutionResult | None = None
    integrity_result: TestIntegrityResult | None = None
    verification_report: VerificationReport | None = None
    acceptance_decision: AcceptanceDecision | None = None
    mutation_report: MutationReport | None = None
    failure_reason: FailureReason | None = None
    feedback: str | None = None


class RepairReport(BaseModel):
    """Final auditable outcome of a single WorkUnit repair."""

    work_unit_id: str
    execution_mode: ExecutionMode
    attempts: list[RepairAttempt] = Field(default_factory=list)
    final_status: RepairState
    accepted_diff: str | None = None
    root_cause_explanation: str | None = None
    test_integrity_passed: bool = False
    targeted_test_passed: bool = False
    verification_report: VerificationReport | None = None
    acceptance_decision: AcceptanceDecision | None = None
    mutation_report: MutationReport | None = None
    acceptance_reason: str | None = None
    total_duration_seconds: float = 0.0
    limitations: list[str] = Field(default_factory=list)


# Short aliases make the schema convenient for integrations without changing
# the canonical serialized class names above.
RegressionTest = RegressionTestResult
