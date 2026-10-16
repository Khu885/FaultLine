"""Typed data contracts for FaultLine's deterministic Day 1 pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

LocationStatus = Literal["resolved", "unresolved"]
LocationCategory = Literal["application", "test_assertion", "test", "unresolved"]
LocalizationConfidence = Literal["high", "medium", "low", "unresolved"]


class SourceLocation(BaseModel):
    """An AST-backed source location, or an explicit unresolved result."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: LocationStatus
    path: str | None = None
    line: int | None = None
    function: str | None = None
    source_snippet: str | None = None
    confidence: LocalizationConfidence = "unresolved"
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
    """One failed pytest node, including deterministic localization."""

    model_config = ConfigDict(extra="ignore")

    test_node_id: str = Field(validation_alias=AliasChoices("test_node_id", "nodeid"))
    exception_type: str | None = None
    message: str = ""
    traceback: str = ""
    source_location: SourceLocation | None = None
    phase: str = "call"

    @property
    def nodeid(self) -> str:
        return self.test_node_id

    @property
    def test_name(self) -> str:
        return self.test_node_id

    @property
    def source_file(self) -> str | None:
        return self.source_location.path if self.source_location else None

    @property
    def source_path(self) -> str | None:
        return self.source_file

    @property
    def line(self) -> int | None:
        return self.source_location.line if self.source_location else None

    @property
    def function(self) -> str | None:
        return self.source_location.function if self.source_location else None

    @property
    def source_snippet(self) -> str | None:
        return self.source_location.source_snippet if self.source_location else None

    @property
    def localization_confidence(self) -> LocalizationConfidence:
        return self.source_location.confidence if self.source_location else "unresolved"

    @property
    def unresolved_reason(self) -> str | None:
        return self.source_location.reason if self.source_location else "no source location"


class WorkUnit(BaseModel):
    """Conservative grouping by source file, not a proof of independence."""

    model_config = ConfigDict(extra="ignore")

    work_unit_id: str | None = None
    source_file: str | None = None
    failures: list[FailureRecord] = Field(default_factory=list)
    grouping_note: str = (
        "Failures sharing a source file are grouped conservatively; this does not guarantee "
        "independence or a shared root cause across different files."
    )

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def identifier(self) -> str:
        return self.work_unit_id or self.source_file or (self.failures[0].test_node_id if self.failures else "empty")

    @property
    def test_node_ids(self) -> list[str]:
        return [failure.test_node_id for failure in self.failures]


class ScanReport(BaseModel):
    """Complete deterministic result of one FaultLine scan."""

    model_config = ConfigDict(extra="ignore")

    project_path: str
    failures: list[FailureRecord] = Field(default_factory=list)
    work_units: list[WorkUnit] = Field(default_factory=list)
    collection_error: str | None = None
    report_error: str | None = None
    test_count: int = 0
    exit_code: int = 0
    timed_out: bool = False

    @property
    def test_failures(self) -> list[FailureRecord]:
        return self.failures

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    @property
    def is_empty(self) -> bool:
        return self.test_count == 0 and not self.failures and self.collection_error is None
