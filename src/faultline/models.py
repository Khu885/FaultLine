"""Pydantic models shared by FaultLine's deterministic pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LocationStatus = Literal["resolved", "unresolved"]
LocationCategory = Literal["application", "test_assertion", "test", "unresolved"]


class ExecutionMode(StrEnum):
    """How a repair proposal was produced."""

    FIXTURE = "offline_fixture"
    OMNIRUSH_HUMAN_MEDIATED = "omnirush_human_mediated"
    OMNIRUSH_AUTOMATED = "omnirush_automated"
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
    FAILED = "FAILED"
    REJECTED = "REJECTED"


FailureReason = Literal[
    "agent_unavailable",
    "invalid_proposal",
    "patch_rejected",
    "syntax_error",
    "test_assertion_failure",
    "test_timeout",
    "test_collection_failure",
    "test_integrity_violation",
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


class RepairAttempt(BaseModel):
    """Evidence from one independent candidate generation attempt."""

    attempt_number: int
    state_history: list[RepairState] = Field(default_factory=list)
    proposal: PatchProposal | None = None
    validation_result: PatchValidationResult | None = None
    test_result: TestExecutionResult | None = None
    integrity_result: TestIntegrityResult | None = None
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
    total_duration_seconds: float = 0.0
    limitations: list[str] = Field(default_factory=list)
