"""Deterministic, conservative failure grouping."""

from __future__ import annotations

from collections import OrderedDict

from .models import FailureRecord, WorkUnit


def group_work_units(failures: list[FailureRecord] | tuple[FailureRecord, ...]) -> list[WorkUnit]:
    """Group only failures with the same resolved source file.

    Unresolved failures remain separate because a missing location is not
    evidence that two failures share a cause. Grouping by file is conservative
    and does not guarantee independence or a shared root cause.
    """
    grouped: OrderedDict[str, list[FailureRecord]] = OrderedDict()
    for index, failure in enumerate(failures):
        source_file = failure.source_file
        key = source_file if source_file else f"__unresolved__:{index}"
        grouped.setdefault(key, []).append(failure)
    return [
        WorkUnit(
            work_unit_id=key,
            source_file=None if key.startswith("__unresolved__:") else key,
            failures=items,
        )
        for key, items in grouped.items()
    ]
