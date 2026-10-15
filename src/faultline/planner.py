"""Deterministic, conservative failure grouping."""

from __future__ import annotations

from collections import OrderedDict

from .models import FailureRecord, WorkUnit


def group_work_units(failures: list[FailureRecord] | tuple[FailureRecord, ...]) -> list[WorkUnit]:
    """Group failures by resolved source file without inferring shared causes.

    Failures without a resolved source location each receive their own unit, so
    unrelated or ambiguous failures are never silently merged.
    """
    grouped: OrderedDict[str, list[FailureRecord]] = OrderedDict()
    for index, failure in enumerate(failures):
        source_file = failure.source_file
        key = source_file if source_file else f"__unresolved__:{index}"
        grouped.setdefault(key, []).append(failure)
    return [
        WorkUnit(source_file=None if key.startswith("__unresolved__:") else key, failures=items)
        for key, items in grouped.items()
    ]
