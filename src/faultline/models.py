"""Pydantic models shared by FaultLine's deterministic pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LocationStatus = Literal["resolved", "unresolved"]
LocationCategory = Literal["application", "test_assertion", "test", "unresolved"]


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

    source_file: str | None = None
    failures: list[FailureRecord] = Field(default_factory=list)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


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
