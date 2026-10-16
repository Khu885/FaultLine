"""Deterministic, conservative failure grouping."""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256

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
        WorkUnit(
            work_unit_id=sha256(
                "|".join([key, *(failure.nodeid for failure in items)]).encode("utf-8")
            ).hexdigest()[:16],
            source_file=None if key.startswith("__unresolved__:") else key,
            target_function=(
                items[0].source_location.function
                if items and items[0].source_location is not None
                else None
            ),
            test_node_ids=[failure.nodeid for failure in items],
            failures=items,
        )
        for key, items in grouped.items()
    ]
