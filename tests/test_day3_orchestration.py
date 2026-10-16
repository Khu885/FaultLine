import threading
import time

from faultline.lifecycle import (
    CleanupHooks,
    InvalidTransitionError,
    WorkerCancelledError,
    WorkerLifecycle,
    WorkerState,
)
from faultline.models import ExecutionMode, RepairReport, RepairState, WorkUnit
from faultline.orchestrator import RepairOrchestrator


def unit(name: str) -> WorkUnit:
    return WorkUnit(work_unit_id=name, test_node_ids=[f"{name}.py::test_case"])


def report(name: str, state: RepairState) -> RepairReport:
    return RepairReport(work_unit_id=name, execution_mode=ExecutionMode.FIXTURE, final_status=state)


def test_bounded_concurrency_and_input_order():
    active = maximum = 0
    lock = threading.Lock()

    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, work_unit):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return report(work_unit.identifier, RepairState.PROVISIONALLY_SUCCESSFUL)

    result = RepairOrchestrator(Worker(), max_workers=2).execute(".", [unit("a"), unit("b"), unit("c")])
    assert maximum == 2
    assert result.work_unit_ids == ["a", "b", "c"]
    assert result.successful_count == 3


def test_worker_exception_isolated():
    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, work_unit):
            if work_unit.identifier == "bad":
                raise RuntimeError("boom")
            return report(work_unit.identifier, RepairState.PROVISIONALLY_SUCCESSFUL)

    result = RepairOrchestrator(Worker(), max_workers=3).execute(".", [unit("ok1"), unit("bad"), unit("ok2")])
    assert [item.status for item in result.results] == ["success", "failure", "success"]
    assert "RuntimeError: boom" in (result.results[1].error or "")


def test_lifecycle_rejects_illegal_transition_and_cleanup_is_lifo():
    lifecycle = WorkerLifecycle()
    try:
        lifecycle.transition(WorkerState.SUCCEEDED)
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("illegal transition accepted")
    calls = []
    hooks = CleanupHooks([lambda: calls.append("first"), lambda: calls.append("second")])
    hooks.run()
    hooks.run()
    assert calls == ["second", "first"]


def test_three_work_unit_demo_records_success_failure_and_rejection():
    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, work_unit):
            state = {
                "success": RepairState.PROVISIONALLY_SUCCESSFUL,
                "failure": RepairState.FAILED,
                "rejected": RepairState.REJECTED,
            }[work_unit.identifier]
            return report(work_unit.identifier, state)

    result = RepairOrchestrator(Worker(), max_workers=3).execute(
        ".", [unit("success"), unit("failure"), unit("rejected")]
    )
    assert [item.status for item in result.results] == ["success", "failure", "rejected"]
    assert result.successful_count == 1
    assert result.failed_count == 1
    assert result.rejected_count == 1


def test_timeout_is_measured_from_worker_start_and_queued_work_is_not_timed_out():
    started = threading.Event()

    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, work_unit, *, cancellation_event):
            if work_unit.identifier == "slow":
                started.set()
                while not cancellation_event.wait(0.002):
                    pass
                raise WorkerCancelledError("slow worker observed timeout")
            return report(work_unit.identifier, RepairState.PROVISIONALLY_SUCCESSFUL)

    result = RepairOrchestrator(Worker(), max_workers=1, worker_timeout=0.03).execute(
        ".", [unit("slow"), unit("queued")]
    )

    assert started.is_set()
    assert [item.status for item in result.results] == ["timed_out", "success"]
    assert result.results[0].duration_seconds >= 0.02


def test_cancel_signals_active_worker_cooperatively():
    entered = threading.Event()
    result_holder = []

    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, _work_unit, *, cancellation_event):
            entered.set()
            while not cancellation_event.wait(0.002):
                pass
            raise WorkerCancelledError("active cancellation observed")

    orchestrator = RepairOrchestrator(Worker(), max_workers=1)

    def run_batch():
        result_holder.append(orchestrator.execute(".", [unit("active")]))

    thread = threading.Thread(target=run_batch)
    thread.start()
    assert entered.wait(1)
    orchestrator.cancel()
    thread.join(1)

    assert not thread.is_alive()
    assert result_holder[0].results[0].status == "cancelled"


def test_runtime_error_containing_cancel_is_still_a_failure():
    class Worker:
        agent = type("Agent", (), {"execution_mode": ExecutionMode.FIXTURE})()

        def repair(self, _root, _work_unit):
            raise RuntimeError("downstream cancellation-like failure")

    result = RepairOrchestrator(Worker()).execute(".", [unit("error")])

    assert result.results[0].status == "failure"
    assert result.results[0].error == "worker raised RuntimeError: downstream cancellation-like failure"
