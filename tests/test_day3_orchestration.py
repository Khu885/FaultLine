import threading
import time

from faultline.lifecycle import CleanupHooks, InvalidTransitionError, WorkerLifecycle, WorkerState
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
