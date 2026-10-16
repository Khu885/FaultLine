"""Bounded parallel orchestration around RepairWorker."""
from __future__ import annotations

import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .lifecycle import WorkerState
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


class RepairOrchestrator:
    def __init__(self, worker: Any, *, max_workers: int = 3, worker_timeout: float | None = None) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.worker = worker
        self.max_workers = max_workers
        self.worker_timeout = worker_timeout
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _invoke(self, root: str | Path, unit: WorkUnit):
        if self._cancel.is_set():
            raise RuntimeError("cancelled before start")
        kwargs = {}
        try:
            parameters = inspect.signature(self.worker.repair).parameters
            if "cancellation_event" in parameters:
                kwargs["cancellation_event"] = self._cancel
            elif "cancel_event" in parameters:
                kwargs["cancel_event"] = self._cancel
        except (TypeError, ValueError):
            pass
        return self.worker.repair(root, unit, **kwargs)

    def execute(self, root: str | Path, units: list[WorkUnit]) -> AggregateRepairReport:
        started = time.monotonic()
        mode = str(getattr(getattr(self.worker, "agent", None), "execution_mode", ExecutionMode.UNAVAILABLE))
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        futures = [executor.submit(self._invoke, root, unit) for unit in units]
        results: list[RepairWorkerOutcome] = []
        try:
            for unit, future in zip(units, futures, strict=True):
                tick = time.monotonic()
                try:
                    report = future.result(timeout=self.worker_timeout)
                    status = "success" if report.final_status is RepairState.PROVISIONALLY_SUCCESSFUL else "rejected" if report.final_status is RepairState.REJECTED else "failure"
                    history = [WorkerState.RUNNING.value, WorkerState.SUCCEEDED.value if status == "success" else WorkerState.FAILED.value]
                    results.append(RepairWorkerOutcome(work_unit_id=unit.identifier, status=status, report=report, duration_seconds=time.monotonic() - tick, state_history=history))
                except TimeoutError:
                    future.cancel()
                    results.append(RepairWorkerOutcome(work_unit_id=unit.identifier, status="timed_out", error=f"worker timed out after {self.worker_timeout:g} seconds", duration_seconds=time.monotonic() - tick, state_history=[WorkerState.RUNNING.value, WorkerState.TIMED_OUT.value]))
                except RuntimeError as exc:
                    status = "cancelled" if "cancel" in str(exc).lower() else "failure"
                    error = "cancelled before start" if status == "cancelled" else f"worker raised {type(exc).__name__}: {exc}"
                    results.append(RepairWorkerOutcome(work_unit_id=unit.identifier, status=status, error=error, duration_seconds=time.monotonic() - tick, state_history=[WorkerState.PENDING.value, WorkerState.CANCELLED.value if status == "cancelled" else WorkerState.FAILED.value]))
                except Exception as exc:
                    results.append(RepairWorkerOutcome(work_unit_id=unit.identifier, status="failure", error=f"worker raised {type(exc).__name__}: {exc}", duration_seconds=time.monotonic() - tick, state_history=[WorkerState.RUNNING.value, WorkerState.FAILED.value]))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return AggregateRepairReport(project_path=str(root), execution_mode=mode, results=results, total_work_units=len(results), successful_repairs=sum(item.status == "success" for item in results), failed_repairs=sum(item.status == "failure" for item in results), rejected_patches=sum(item.status == "rejected" for item in results), cancelled_workers=sum(item.status == "cancelled" for item in results), timed_out_workers=sum(item.status == "timed_out" for item in results), total_duration_seconds=time.monotonic() - started, max_workers=self.max_workers)

    repair = execute
    run = execute
