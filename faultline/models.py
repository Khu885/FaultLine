"""Typed values shared by FaultLine analysis features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


LocationStatus = Literal["resolved", "unresolved"]
LocationCategory = Literal["application", "test_assertion", "test", "unresolved"]


@dataclass(frozen=True)
class SourceLocation:
    """The result of mapping a traceback frame to Python source."""

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
