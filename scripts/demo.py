"""Run FaultLine's reproducible, offline submission demonstration.

The demo deliberately keeps the live OmniRush handoff separate from the
automated path.  No network call or agent process is started here.  The repair
proposal used below is an explicit ``offline_fixture`` proposal so the command
can run without credentials while still exercising the real worker, arbiter,
regression, integrity, and mutation code paths.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from faultline.agent_adapter import FixtureAgentAdapter, build_repair_request
from faultline.collector import collect_project
from faultline.models import (
    ExecutionMode,
    FailureRecord,
    PatchProposal,
    RepairState,
    SourceLocation,
    WorkUnit,
)
from faultline.worker import RepairWorker


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
OUTPUT = ROOT / "demo-output"
PAGINATION_SOURCE = "off_by_one_pagination.py"
PAGINATION_TEST = "test_off_by_one_pagination.py"
PAGINATION_NODE = f"{PAGINATION_TEST}::test_pagination_returns_every_item_on_the_requested_page"


class DemoFailure(RuntimeError):
    """Raised when the demonstration does not produce its expected evidence."""


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _copy_benchmark_subset(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("__init__.py", PAGINATION_SOURCE, PAGINATION_TEST):
        shutil.copy2(BENCHMARKS / filename, destination / filename)


def _pagination_unit(project: Path) -> WorkUnit:
    return WorkUnit(
        work_unit_id="pagination-demo",
        source_file=str(project / PAGINATION_SOURCE),
        target_function="paginate",
        test_node_ids=[PAGINATION_NODE],
        failures=[
            FailureRecord(
                nodeid=PAGINATION_NODE,
                traceback="AssertionError: the final page item is missing",
                message="AssertionError: the final page item is missing",
                source_location=SourceLocation(
                    status="resolved",
                    path=str(project / PAGINATION_SOURCE),
                    line=8,
                    function="paginate",
                ),
            )
        ],
    )


def _pagination_proposal(work_unit_id: str) -> PatchProposal:
    return PatchProposal(
        work_unit_id=work_unit_id,
        target_file=PAGINATION_SOURCE,
        root_cause_explanation="The slice endpoint omitted the final item on a full page.",
        unified_diff=(
            "--- a/off_by_one_pagination.py\n"
            "+++ b/off_by_one_pagination.py\n"
            "@@ -6,3 +6,3 @@\n"
            "     start = page * page_size\n"
            "-    stop = start + page_size - 1\n"
            "+    stop = start + page_size\n"
            "     return items[start:stop]\n"
        ),
        attempt_number=1,
        execution_mode=ExecutionMode.FIXTURE,
    )


def _simple_unit(project: Path, *, work_unit_id: str, node_id: str) -> WorkUnit:
    return WorkUnit(
        work_unit_id=work_unit_id,
        source_file=str(project / "app.py"),
        target_function="value",
        test_node_ids=[node_id],
        failures=[
            FailureRecord(
                nodeid=node_id,
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved",
                    path=str(project / "app.py"),
                    line=2,
                    function="value",
                ),
            )
        ],
    )


def _simple_proposal(work_unit_id: str, replacement: str) -> PatchProposal:
    return PatchProposal(
        work_unit_id=work_unit_id,
        target_file="app.py",
        root_cause_explanation="The implementation returned the wrong value.",
        unified_diff=(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def value():\n"
            f"-    return {replacement[0]}\n"
            f"+    return {replacement[1]}\n"
        ),
        attempt_number=1,
        execution_mode=ExecutionMode.FIXTURE,
    )


def _request_for_unit(project: Path, unit: WorkUnit) -> dict[str, object]:
    source_path = Path(unit.source_file or "")
    source = source_path.read_text(encoding="utf-8")
    line = unit.failures[0].source_location.line if unit.failures and unit.failures[0].source_location else 1
    relative = source_path.relative_to(project).as_posix()
    request = build_repair_request(
        unit,
        source_snippet="\n".join(source.splitlines()[max(0, line - 8): min(len(source.splitlines()), line + 7)]),
        authorized_paths=[relative],
    ).model_copy(update={"target_file": relative})
    return request.model_dump(mode="json")


def _save_report(name: str, report: object) -> Path:
    path = OUTPUT / name
    payload = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
    _write_json(path, payload)
    return path


def _print_scan(scan) -> None:
    print("\n[1/5] Failure detection")
    print(f"  failures={len(scan.failures)} work_units={len(scan.work_units)} exit_code={scan.exit_code}")
    for failure in scan.failures:
        location = failure.source_location
        location_text = (
            f"{location.path}:{location.line}" if location and location.path and location.line else "unresolved"
        )
        print(f"  FAIL {failure.nodeid} -> {location_text}")


def run_demo() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for filename in (
        "scan-report.json",
        "pagination-repair-request.json",
        "pagination-repair-report.json",
        "regression-rejection-report.json",
        "inconclusive-report.json",
    ):
        path = OUTPUT / filename
        if path.exists():
            path.unlink()

    original_benchmark = (BENCHMARKS / PAGINATION_SOURCE).read_text(encoding="utf-8")
    scan = collect_project(BENCHMARKS)
    _save_report("scan-report.json", scan)
    _print_scan(scan)
    if len(scan.failures) != 3 or len(scan.work_units) != 3:
        raise DemoFailure("benchmark scan did not produce the expected three failures and WorkUnits")

    pagination_scan_unit = next(
        (unit for unit in scan.work_units if unit.target_function == "paginate"),
        None,
    )
    if pagination_scan_unit is None:
        raise DemoFailure("pagination WorkUnit was not found in the benchmark scan")

    print("\n[2/5] OmniRush handoff and accepted offline verification")
    print("  live OmniRush call: not started; this checkout has no authentic proposal artifact")
    print("  automated proposal mode: offline_fixture (explicit deterministic fixture)")
    with tempfile.TemporaryDirectory(prefix="faultline-demo-pagination-") as raw:
        project = Path(raw) / "benchmarks"
        _copy_benchmark_subset(project)
        unit = _pagination_unit(project)
        _write_json(OUTPUT / "pagination-repair-request.json", _request_for_unit(project, unit))
        proposal = _pagination_proposal(unit.identifier)
        before = (project / PAGINATION_SOURCE).read_text(encoding="utf-8")
        report = RepairWorker(
            FixtureAgentAdapter([proposal]),
            max_attempts=1,
            timeout=10,
            regression_timeout=30,
            mutation_test=True,
            max_mutants=5,
        ).repair(project, unit)
        _save_report("pagination-repair-report.json", report)
        mutation = report.mutation_report
        score = f"{mutation.kill_rate:.2f}%" if mutation and mutation.kill_rate is not None else "unavailable"
        print(f"  final_status={report.final_status.value}")
        print(f"  targeted_test_passed={report.targeted_test_passed} test_integrity_passed={report.test_integrity_passed}")
        print(
            "  regression_passed="
            f"{report.verification_report.regression_passed if report.verification_report else False}"
        )
        if mutation is not None:
            print(
                f"  mutants generated={mutation.total_generated} killed={mutation.killed} "
                f"survived={mutation.survived} inconclusive={mutation.inconclusive} "
                f"kill_rate={score} duration={mutation.duration_seconds:.2f}s"
            )
        if report.final_status is not RepairState.ACCEPTED:
            raise DemoFailure(f"pagination repair was not accepted: {report.final_status.value}")
        if (project / PAGINATION_SOURCE).read_text(encoding="utf-8") != before:
            raise DemoFailure("candidate workspace was modified by verification")

    print("\n[3/5] Regression-causing patch rejection")
    with tempfile.TemporaryDirectory(prefix="faultline-demo-rejection-") as raw:
        project = Path(raw)
        (project / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (project / "test_app.py").write_text(
            "from app import value\n\n"
            "def test_target():\n    assert value() == 2\n\n"
            "def test_previously_passing():\n    assert value() == 1\n",
            encoding="utf-8",
        )
        unit = _simple_unit(project, work_unit_id="regression-rejection", node_id="test_app.py::test_target")
        before = (project / "app.py").read_text(encoding="utf-8")
        report = RepairWorker(
            FixtureAgentAdapter([_simple_proposal(unit.identifier, "12")]),
            max_attempts=1,
            timeout=10,
            regression_timeout=30,
        ).repair(project, unit)
        _save_report("regression-rejection-report.json", report)
        print(
            f"  final_status={report.final_status.value} targeted_test_passed={report.targeted_test_passed} "
            f"newly_failing={report.verification_report.newly_failing if report.verification_report else []}"
        )
        if report.final_status is not RepairState.REJECTED:
            raise DemoFailure(f"regression-causing patch was not rejected: {report.final_status.value}")
        if (project / "app.py").read_text(encoding="utf-8") != before:
            raise DemoFailure("rejection demo modified its original candidate project")

    print("\n[4/5] Incomplete verification")
    with tempfile.TemporaryDirectory(prefix="faultline-demo-inconclusive-") as raw:
        project = Path(raw)
        (project / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (project / "test_app.py").write_text(
            "import time\nfrom app import value\n\n"
            "def test_value():\n    time.sleep(0.2)\n    assert value() == 2\n",
            encoding="utf-8",
        )
        unit = _simple_unit(project, work_unit_id="inconclusive", node_id="test_app.py::test_value")
        report = RepairWorker(
            FixtureAgentAdapter([_simple_proposal(unit.identifier, "12")]),
            max_attempts=1,
            timeout=10,
            regression_timeout=0.01,
        ).repair(project, unit)
        _save_report("inconclusive-report.json", report)
        print(f"  final_status={report.final_status.value} acceptance_reason={report.acceptance_reason}")
        if report.final_status is not RepairState.INCONCLUSIVE:
            raise DemoFailure(f"incomplete verification was not inconclusive: {report.final_status.value}")

    if (BENCHMARKS / PAGINATION_SOURCE).read_text(encoding="utf-8") != original_benchmark:
        raise DemoFailure("original benchmark source changed during the demo")

    print("\n[5/5] Demo complete")
    print(f"  structured reports: {OUTPUT}")
    print("  no benchmark source or test file was modified")
    print("  human action remains required for a genuine OmniRush proposal handoff")
    return 0


def main() -> int:
    try:
        return run_demo()
    except (DemoFailure, OSError, UnicodeError, ValueError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
