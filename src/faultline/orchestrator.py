"""Bounded parallel orchestration around RepairWorker."""
from __future__ import annotations

import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .lifecycle import WorkerCancelledError, WorkerState, raise_if_cancelled
from .models import ExecutionMode, RepairReport, RepairState, WorkUnit


class RepairWorkerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_unit_id: str
    status: str
    report: RepairReport | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    state_history: list[str] = Field(default_factory=list)


class AggregateRepairReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_path: str
    execution_mode: str
    results: list[RepairWorkerOutcome] = Field(default_factory=list)
    total_work_units: int = 0
    successful_repairs: int = 0
    failed_repairs: int = 0
    rejected_patches: int = 0
    cancelled_workers: int = 0
    timed_out_workers: int = 0
    total_duration_seconds: float = 0.0
    max_workers: int = 1
    limitations: list[str] = Field(default_factory=lambda: [
        "Worker lifecycle state is in-memory and not durable across restart.",
        "Temporary copies are not a hardened OS sandbox.",
        "Python threads cannot be forcefully stopped; active timeout and cancellation depend on cooperative worker checks.",
    ])

    @property
    def reports(self):
        return [item.report for item in self.results]

    @property
    def work_unit_ids(self):
        return [item.work_unit_id for item in self.results]

    @property
    def successful_count(self):
        return self.successful_repairs

    @property
    def failed_count(self):
        return self.failed_repairs

    @property
    def rejected_count(self):
        return self.rejected_patches

    def __iter__(self):
        return iter(self.reports)


@dataclass(eq=False)
class _Invocation:
    """Execution metadata kept separately from the public report models."""

    unit: WorkUnit
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    future: object | None = None
    outcome: RepairWorkerOutcome | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_started(self) -> None:
        with self._lock:
            if self.started_at is None:
                self.started_at = time.monotonic()

    def start_time(self) -> float | None:
        with self._lock:
            return self.started_at


class RepairOrchestrator:
    def __init__(self, worker: Any, *, max_workers: int = 3, worker_timeout: float | None = None) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.worker = worker
        self.max_workers = max_workers
        self.worker_timeout = worker_timeout
        self._cancel = threading.Event()
        self._invocation_lock = threading.Lock()
        self._invocations: list[_Invocation] = []

    def cancel(self) -> None:
        self._cancel.set()
        with self._invocation_lock:
            for invocation in self._invocations:
                invocation.cancellation_event.set()

    def _invoke(
        self,
        root: str | Path,
        unit: WorkUnit,
        *,
        cancellation_event: threading.Event | None = None,
        on_start=None,
    ):
        event = cancellation_event or self._cancel
        raise_if_cancelled(event, "worker cancelled before start")
        if on_start is not None:
            on_start()
        raise_if_cancelled(event, "worker cancelled before start")
        kwargs = {}
        try:
            parameters = inspect.signature(self.worker.repair).parameters
            if "cancellation_event" in parameters:
                kwargs["cancellation_event"] = event
            elif "cancel_event" in parameters:
                kwargs["cancel_event"] = event
        except (TypeError, ValueError):
            pass
        return self.worker.repair(root, unit, **kwargs)

    @staticmethod
    def _duration(invocation: _Invocation) -> float:
        return max(0.0, time.monotonic() - (invocation.start_time() or invocation.submitted_at))

    def _report_outcome(self, invocation: _Invocation, report: RepairReport) -> RepairWorkerOutcome:
        status = (
            "success"
            if report.final_status is RepairState.PROVISIONALLY_SUCCESSFUL
            else "rejected"
            if report.final_status is RepairState.REJECTED
            else "failure"
        )
        terminal = WorkerState.SUCCEEDED.value if status == "success" else WorkerState.FAILED.value
        return RepairWorkerOutcome(
            work_unit_id=invocation.unit.identifier,
            status=status,
            report=report,
            duration_seconds=self._duration(invocation),
            state_history=[WorkerState.RUNNING.value, terminal],
        )

    def _exception_outcome(self, invocation: _Invocation, exc: BaseException) -> RepairWorkerOutcome:
        if isinstance(exc, (WorkerCancelledError, FutureCancelledError)):
            started = invocation.start_time() is not None
            return RepairWorkerOutcome(
                work_unit_id=invocation.unit.identifier,
                status="cancelled",
                error=str(exc) or "worker cancelled",
                duration_seconds=self._duration(invocation),
                state_history=[
                    WorkerState.RUNNING.value if started else WorkerState.PENDING.value,
                    WorkerState.CANCELLED.value,
                ],
            )
        return RepairWorkerOutcome(
            work_unit_id=invocation.unit.identifier,
            status="failure",
            error=f"worker raised {type(exc).__name__}: {exc}",
            duration_seconds=self._duration(invocation),
            state_history=[
                WorkerState.RUNNING.value if invocation.start_time() is not None else WorkerState.PENDING.value,
                WorkerState.FAILED.value,
            ],
        )

    def _cancel_queued_when_no_slot(self, invocations: list[_Invocation]) -> None:
        """Avoid waiting forever when a timed-out thread ignores its signal."""
        # Give cooperative workers a few polling turns to observe the event and
        # release their executor slot before abandoning queued work.
        now = time.monotonic()
        blocked = sum(
            invocation.start_time() is not None
            and invocation.outcome is not None
            and not invocation.future.done()
            and now - invocation.start_time() >= (self.worker_timeout or 0.0) + 0.05
            for invocation in invocations
            if invocation.future is not None
        )
        if blocked < self.max_workers:
            return
        for invocation in invocations:
            future = invocation.future
            if invocation.outcome is None and future is not None and invocation.start_time() is None and future.cancel():
                invocation.outcome = RepairWorkerOutcome(
                    work_unit_id=invocation.unit.identifier,
                    status="cancelled",
                    error="queued worker cancelled after available worker slots were exhausted",
                    duration_seconds=self._duration(invocation),
                    state_history=[WorkerState.PENDING.value, WorkerState.CANCELLED.value],
                )

    def execute(self, root: str | Path, units: list[WorkUnit]) -> AggregateRepairReport:
        started = time.monotonic()
        mode = str(getattr(getattr(self.worker, "agent", None), "execution_mode", ExecutionMode.UNAVAILABLE))
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        invocations = [_Invocation(unit) for unit in units]
        with self._invocation_lock:
            self._invocations.extend(invocations)
        for invocation in invocations:
            invocation.future = executor.submit(
                self._invoke,
                root,
                invocation.unit,
                cancellation_event=invocation.cancellation_event,
                on_start=invocation.mark_started,
            )
        try:
            pending = set(invocations)
            while pending:
                now = time.monotonic()
                for invocation in tuple(pending):
                    future = invocation.future
                    if future is None:
                        continue
                    if invocation.outcome is not None:
                        pending.discard(invocation)
                        continue
                    if future.cancelled():
                        invocation.outcome = self._exception_outcome(invocation, FutureCancelledError())
                    elif future.done():
                        try:
                            invocation.outcome = self._report_outcome(invocation, future.result())
                        except Exception as exc:
                            invocation.outcome = self._exception_outcome(invocation, exc)
                    elif self._cancel.is_set():
                        invocation.cancellation_event.set()
                        if invocation.start_time() is None and future.cancel():
                            invocation.outcome = self._exception_outcome(invocation, FutureCancelledError())
                        elif invocation.start_time() is not None:
                            invocation.outcome = RepairWorkerOutcome(
                                work_unit_id=invocation.unit.identifier,
                                status="cancelled",
                                error="worker cancellation requested",
                                duration_seconds=self._duration(invocation),
                                state_history=[WorkerState.RUNNING.value, WorkerState.CANCELLED.value],
                            )
                    elif self.worker_timeout is not None:
                        started_at = invocation.start_time()
                        if started_at is not None and now - started_at >= self.worker_timeout:
                            invocation.cancellation_event.set()
                            future.cancel()
                            invocation.outcome = RepairWorkerOutcome(
                                work_unit_id=invocation.unit.identifier,
                                status="timed_out",
                                error=f"worker timed out after {self.worker_timeout:g} seconds",
                                duration_seconds=max(0.0, now - started_at),
                                state_history=[WorkerState.RUNNING.value, WorkerState.TIMED_OUT.value],
                            )
                    if invocation.outcome is not None:
                        pending.discard(invocation)
                self._cancel_queued_when_no_slot(invocations)
                for invocation in tuple(pending):
                    if invocation.outcome is not None:
                        pending.discard(invocation)
                if pending:
                    time.sleep(0.005)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            with self._invocation_lock:
                for invocation in invocations:
                    if invocation in self._invocations:
                        self._invocations.remove(invocation)
        results = [invocation.outcome for invocation in invocations]
        assert all(outcome is not None for outcome in results)
        ordered_results = [outcome for outcome in results if outcome is not None]
        return AggregateRepairReport(project_path=str(root), execution_mode=mode, results=ordered_results, total_work_units=len(ordered_results), successful_repairs=sum(item.status == "success" for item in ordered_results), failed_repairs=sum(item.status == "failure" for item in ordered_results), rejected_patches=sum(item.status == "rejected" for item in ordered_results), cancelled_workers=sum(item.status == "cancelled" for item in ordered_results), timed_out_workers=sum(item.status == "timed_out" for item in ordered_results), total_duration_seconds=time.monotonic() - started, max_workers=self.max_workers)

    repair = execute
    run = execute
