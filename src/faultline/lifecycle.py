from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum


class WorkerState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LifecycleEvent:
    state: WorkerState
    timestamp: float
    reason: str | None = None


class WorkerLifecycle:
    _allowed = {
        WorkerState.PENDING: {WorkerState.RUNNING, WorkerState.CANCELLED},
        WorkerState.RUNNING: {WorkerState.SUCCEEDED, WorkerState.FAILED, WorkerState.CANCELLED, WorkerState.TIMED_OUT},
    }

    def __init__(self) -> None:
        self._state = WorkerState.PENDING
        self._events = [LifecycleEvent(self._state, time.monotonic())]
        self._lock = threading.Lock()

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._events)

    def transition(self, state: WorkerState, reason: str | None = None) -> None:
        with self._lock:
            if state not in self._allowed.get(self._state, set()):
                raise InvalidTransitionError(f"invalid transition {self._state} -> {state}")
            self._state = state
            self._events.append(LifecycleEvent(state, time.monotonic(), reason))


class CleanupHooks:
    def __init__(self, hooks=None) -> None:
        self._hooks = list(hooks or [])
        self._ran = False

    def run(self) -> None:
        if self._ran:
            return
        self._ran = True
        for hook in reversed(self._hooks):
            hook()
