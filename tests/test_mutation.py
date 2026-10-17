import json
import shutil
from pathlib import Path

import pytest

from faultline.agent_adapter import FixtureAgentAdapter
from faultline.cli import main
from faultline.models import (
    ExecutionMode,
    FailureRecord,
    MutationOutcome,
    MutationReport,
    MutationResult,
    SourceLocation,
    WorkUnit,
)
from faultline.mutation import (
    InvalidSourceError,
    calculate_kill_rate,
    generate_mutations,
    run_mutation_testing,
)
from faultline.mutation_runner import (
    INCONCLUSIVE,
    KILLED,
    SURVIVED,
    evaluate_mutant,
)
from faultline.regression import capture_baseline
from faultline.worker import RepairWorker


def _unit(function: str = "value", *, source_file: str = "app.py") -> WorkUnit:
    return WorkUnit(
        work_unit_id="mutation-unit",
        source_file=source_file,
        target_function=function,
        test_node_ids=["test_app.py::test_value"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_value",
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved",
                    path=source_file,
                    line=2,
                    function=function,
                ),
            )
        ],
    )


def _mutants(source: str, function: str = "value", max_mutants: int = 20):
    return generate_mutations(source, _unit(function), "app.py", max_mutants=max_mutants)


def _project(tmp_path: Path, *, body: str, test_body: str) -> Path:
    (tmp_path / "app.py").write_text(body, encoding="utf-8")
    (tmp_path / "test_app.py").write_text(test_body, encoding="utf-8")
    return tmp_path


def _passed_baseline(project: Path):
    baseline = capture_baseline(project, timeout=10)
    assert baseline.exit_code == 0
    assert baseline.passed_tests
    return baseline


def test_comparison_inversion_generates_expected_operator():
    mutants = _mutants("def value(x):\n    return x > 1\n")

    comparisons = [item for item in mutants if item.operator.value == "comparison_inversion"]

    assert comparisons
    assert any("x <= 1" in item.source for item in comparisons)


def test_arithmetic_replacement_supports_plus_minus_and_multiply():
    mutants = _mutants("def value(x):\n    return x + 1\n")

    arithmetic = [item for item in mutants if item.operator.value == "arithmetic_replacement"]

    assert any("x - 1" in item.source for item in arithmetic)
    assert any("x * 1" in item.source for item in arithmetic)


def test_numeric_constant_perturbation_excludes_booleans():
    mutants = _mutants("def value():\n    return 5\n")

    numeric = [item for item in mutants if item.operator.value == "numeric_perturbation"]
    boolean = [item for item in mutants if item.operator.value == "boolean_inversion"]

    assert any("return 6" in item.source for item in numeric)
    assert not boolean


def test_boolean_inversion_generates_only_boolean_mutations():
    mutants = _mutants("def value():\n    return True\n")

    boolean = [item for item in mutants if item.operator.value == "boolean_inversion"]

    assert len(boolean) == 1
    assert "return False" in boolean[0].source


def test_return_value_replacement_generates_none():
    mutants = _mutants("def value():\n    return 2\n")

    returns = [item for item in mutants if item.operator.value == "return_value_replacement"]

    assert len(returns) == 1
    assert "return None" in returns[0].source


def test_each_mutant_has_one_operator_and_valid_python():
    source = "def value(x):\n    if x > 1:\n        return x + 5\n    return False\n"

    mutants = _mutants(source, max_mutants=20)

    assert mutants
    for mutant in mutants:
        compile(mutant.source, "app.py", "exec")
        assert mutant.original_source == source
        assert mutant.source != source
        assert mutant.mutation_id.startswith("mut-")


def test_mutation_ids_are_deterministic():
    source = "def value(x):\n    return x >= 1\n"

    first = _mutants(source)
    second = _mutants(source)

    assert [item.mutation_id for item in first] == [item.mutation_id for item in second]
    assert [item.source for item in first] == [item.source for item in second]


def test_equivalent_sources_are_deduplicated():
    source = "def value():\n    return 1\n"

    mutants = _mutants(source, max_mutants=20)

    assert len({item.source for item in mutants}) == len(mutants)
    assert len({item.mutation_id for item in mutants}) == len(mutants)


def test_mutation_is_restricted_to_the_exact_target_function():
    source = (
        "def other():\n"
        "    return 10 + 1\n\n"
        "def value():\n"
        "    return 2 + 1\n"
    )

    mutants = _mutants(source)

    assert mutants
    assert all("return 10 + 1" in item.source for item in mutants)


def test_nested_functions_and_classes_are_excluded():
    source = (
        "def value():\n"
        "    def inner():\n"
        "        return 1 + 1\n"
        "    class Nested:\n"
        "        def method(self):\n"
        "            return 2 + 1\n"
        "    return 3\n"
    )

    mutants = _mutants(source, max_mutants=20)

    assert mutants
    assert all("return 1 + 1" in item.source for item in mutants)
    assert all("return 2 + 1" in item.source for item in mutants)


def test_maximum_mutant_count_is_enforced():
    source = "def value(x):\n    return x + 1 if x > 0 else False\n"

    mutants = _mutants(source, max_mutants=2)

    assert len(mutants) == 2


def test_no_valid_mutation_opportunities_returns_empty_list():
    assert _mutants("def value(x):\n    pass\n") == []


def test_invalid_source_is_reported_without_mutating_input():
    source = "def value(:\n"

    with pytest.raises(InvalidSourceError):
        _mutants(source)

    assert source == "def value(:\n"


def test_target_can_be_recovered_from_source_location_metadata():
    unit = WorkUnit(
        work_unit_id="location-unit",
        source_file="app.py",
        target_function=None,
        test_node_ids=["test_app.py::test_value"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_value",
                source_location=SourceLocation(
                    status="resolved", path="app.py", line=2, function="value"
                ),
            )
        ],
    )

    mutants = generate_mutations("def value():\n    return 1\n", unit, "app.py")

    assert mutants
    assert all(item.target_function == "value" for item in mutants)


def test_killed_mutant_is_relative_to_a_passing_candidate_baseline(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    baseline = _passed_baseline(project)

    result = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=20,
    )

    assert result.outcome == KILLED
    assert result.exit_code == 1
    assert (project / "app.py").read_text(encoding="utf-8") == "def value():\n    return 2\n"


def test_surviving_mutant_is_not_counted_as_killed(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    baseline = _passed_baseline(project)

    result = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1 + 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=20,
    )

    assert result.outcome == SURVIVED
    assert result.exit_code == 0


def test_invalid_mutant_source_is_inconclusive(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    baseline = _passed_baseline(project)

    result = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value(:\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
    )

    assert result.outcome == INCONCLUSIVE
    assert "invalid mutant source" in (result.failure_reason or "")


def test_timeout_is_inconclusive(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body=(
            "import time\nfrom app import value\n\n"
            "def test_value():\n    time.sleep(0.2)\n    assert value() == 2\n"
        ),
    )
    baseline = _passed_baseline(project)

    result = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=0.01,
    )

    assert result.outcome == INCONCLUSIVE
    assert result.exit_code == 124
    assert "timed out" in (result.failure_reason or "")


def test_unrelated_existing_failure_does_not_kill_a_surviving_mutant(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body=(
            "from app import value\n\n"
            "def test_value():\n    assert value() == 2\n\n"
            "def test_unrelated_failure():\n    assert False\n"
        ),
    )
    baseline = capture_baseline(project, timeout=10)
    assert baseline.exit_code == 1
    assert "test_app.py::test_value" in baseline.passed_tests

    result = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1 + 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=20,
    )

    assert result.outcome == SURVIVED


def test_each_mutant_uses_a_fresh_copy_and_preserves_candidate(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 2\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    baseline = _passed_baseline(project)
    first = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=20,
    )
    second = evaluate_mutant(
        project,
        target_file="app.py",
        mutant_source="def value():\n    return 1 + 1\n",
        test_node_ids=["test_app.py::test_value"],
        baseline=baseline,
        timeout=20,
    )

    assert first.outcome == KILLED
    assert second.outcome == SURVIVED
    assert (project / "app.py").read_text(encoding="utf-8") == "def value():\n    return 2\n"


def test_kill_rate_calculation_and_zero_denominator():
    assert calculate_kill_rate(4, 1) == 80.0
    assert calculate_kill_rate(3, 0) == 100.0
    assert calculate_kill_rate(0, 5) == 0.0
    assert calculate_kill_rate(0, 0) is None


def test_mutation_report_is_json_serializable():
    report = MutationReport(
        work_unit_id="unit",
        total_generated=1,
        killed=1,
        survived=0,
        inconclusive=0,
        kill_rate=100.0,
        results=[
            MutationResult(
                mutation_id="mut-test",
                operator="boolean_inversion",
                target_file="app.py",
                target_function="value",
                source_line=2,
                outcome=MutationOutcome.KILLED,
                test_node_ids=["test_app.py::test_value"],
                exit_code=1,
            )
        ],
        execution_mode=ExecutionMode.FIXTURE,
    )

    encoded = json.loads(report.model_dump_json())

    assert encoded["results"][0]["outcome"] == "KILLED"
    assert encoded["execution_mode"] == "offline_fixture"


def test_mutation_result_order_is_deterministic(tmp_path):
    project = _project(
        tmp_path,
        body="def value(x):\n    return x + 1 if x > 0 else False\n",
        test_body="from app import value\n\ndef test_value():\n    assert value(1)\n",
    )
    baseline = _passed_baseline(project)
    first = run_mutation_testing(
        project,
        source=(project / "app.py").read_text(encoding="utf-8"),
        work_unit=_unit(),
        target_file="app.py",
        baseline=baseline,
        execution_mode=ExecutionMode.FIXTURE,
        test_node_ids=["test_app.py::test_value"],
        max_mutants=3,
        timeout=20,
    )
    second = run_mutation_testing(
        project,
        source=(project / "app.py").read_text(encoding="utf-8"),
        work_unit=_unit(),
        target_file="app.py",
        baseline=baseline,
        execution_mode=ExecutionMode.FIXTURE,
        test_node_ids=["test_app.py::test_value"],
        max_mutants=3,
        timeout=20,
    )

    assert [item.mutation_id for item in first.results] == [item.mutation_id for item in second.results]


def _proposal(work_unit_id: str, diff: str, *, target_file: str = "app.py"):
    from faultline.models import PatchProposal

    return PatchProposal(
        work_unit_id=work_unit_id,
        target_file=target_file,
        root_cause_explanation="The return value is incorrect.",
        unified_diff=diff,
        attempt_number=1,
        execution_mode=ExecutionMode.FIXTURE,
    )


def _repair_unit(project: Path, *, node_id: str = "test_app.py::test_value") -> WorkUnit:
    return WorkUnit(
        work_unit_id="repair-unit",
        source_file=str(project / "app.py"),
        target_function="value",
        test_node_ids=[node_id],
        failures=[
            FailureRecord(
                nodeid=node_id,
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved", path=str(project / "app.py"), line=2, function="value"
                ),
            )
        ],
    )


def test_mutation_testing_is_disabled_by_default(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 1\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    unit = _repair_unit(project)
    proposal = _proposal(
        unit.identifier,
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    )

    report = RepairWorker(FixtureAgentAdapter([proposal]), max_attempts=1).repair(project, unit)

    assert report.final_status.value == "ACCEPTED"
    assert report.mutation_report is None


def test_mutation_testing_runs_after_mandatory_gates_and_is_attached(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 1\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    unit = _repair_unit(project)
    proposal = _proposal(
        unit.identifier,
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    )

    report = RepairWorker(
        FixtureAgentAdapter([proposal]), max_attempts=1, mutation_test=True, max_mutants=2
    ).repair(project, unit)

    assert report.final_status.value == "ACCEPTED"
    assert report.acceptance_decision is not None and report.acceptance_decision.accepted
    assert report.mutation_report is not None
    assert report.attempts[-1].mutation_report is report.mutation_report
    assert report.mutation_report.total_generated == 2
    assert report.mutation_report.inconclusive == 0


def test_surviving_mutant_is_a_quality_warning_not_an_acceptance_failure(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 0\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() > 0\n",
    )
    unit = _repair_unit(project)
    proposal = _proposal(
        unit.identifier,
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 1\n",
    )

    report = RepairWorker(
        FixtureAgentAdapter([proposal]), max_attempts=1, mutation_test=True, max_mutants=2
    ).repair(project, unit)

    assert report.final_status.value == "ACCEPTED"
    assert report.mutation_report is not None
    assert report.mutation_report.survived >= 1
    assert report.mutation_report.quality_warning


def test_mandatory_regression_failure_remains_authoritative_with_mutation_enabled(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 1\n",
        test_body=(
            "from app import value\n\n"
            "def test_value():\n    assert value() == 2\n\n"
            "def test_other():\n    assert value() == 1\n"
        ),
    )
    unit = _repair_unit(project)
    proposal = _proposal(
        unit.identifier,
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    )

    report = RepairWorker(
        FixtureAgentAdapter([proposal]), max_attempts=1, mutation_test=True, max_mutants=1
    ).repair(project, unit)

    assert report.final_status.value == "REJECTED"
    assert report.verification_report is not None
    assert report.verification_report.newly_failing == ["test_app.py::test_other"]
    assert report.mutation_report is None


def test_no_mutation_opportunities_have_an_unavailable_score(tmp_path):
    project = _project(
        tmp_path,
        body="def value(x):\n    pass\n",
        test_body="from app import value\n\ndef test_value():\n    assert value(2) is None\n",
    )
    baseline = _passed_baseline(project)

    report = run_mutation_testing(
        project,
        source=(project / "app.py").read_text(encoding="utf-8"),
        work_unit=_unit(),
        target_file="app.py",
        baseline=baseline,
        execution_mode=ExecutionMode.FIXTURE,
        test_node_ids=["test_app.py::test_value"],
    )

    assert report.total_generated == 0
    assert report.kill_rate is None
    assert report.quality_warning and "unavailable" in report.quality_warning


def test_cli_repair_help_exposes_optional_mutation_controls(capsys):
    result = main(["repair", "--help"])

    output = capsys.readouterr().out
    assert result == 0
    assert "--mutation-test" in output
    assert "--max-mutants" in output


def test_cli_repair_import_runs_optional_mutation_stage(tmp_path):
    project = _project(
        tmp_path,
        body="def value():\n    return 1\n",
        test_body="from app import value\n\ndef test_value():\n    assert value() == 2\n",
    )
    scan_path = project / "scan.json"
    assert main(["scan", str(project), "--json-output", str(scan_path)]) == 0
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    work_unit_id = scan["work_units"][0]["work_unit_id"]
    proposal_path = project / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "work_unit_id": work_unit_id,
                "target_file": "app.py",
                "root_cause_explanation": "The return value is incorrect.",
                "unified_diff": (
                    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
                    " def value():\n-    return 1\n+    return 2\n"
                ),
                "attempt_number": 1,
                "execution_mode": "offline_fixture",
            }
        ),
        encoding="utf-8",
    )
    report_path = project / "repair.json"

    result = main(
        [
            "repair-import",
            str(project),
            "--proposal",
            str(proposal_path),
            "--max-attempts",
            "1",
            "--mutation-test",
            "--max-mutants",
            "1",
            "--json-output",
            str(report_path),
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "ACCEPTED"
    assert report["mutation_report"]["total_generated"] == 1


def test_pagination_repair_produces_auditable_mutation_evidence(tmp_path):
    source_root = Path(__file__).parents[1] / "benchmarks"
    project = tmp_path / "benchmarks"
    project.mkdir()
    for filename in (
        "__init__.py",
        "off_by_one_pagination.py",
        "test_off_by_one_pagination.py",
    ):
        shutil.copy2(source_root / filename, project / filename)
    original_source = (project / "off_by_one_pagination.py").read_text(encoding="utf-8")
    unit = WorkUnit(
        work_unit_id="pagination",
        source_file=str(project / "off_by_one_pagination.py"),
        target_function="paginate",
        test_node_ids=["test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page"],
        failures=[
            FailureRecord(
                nodeid="test_off_by_one_pagination.py::test_pagination_returns_every_item_on_the_requested_page",
                traceback="AssertionError",
                source_location=SourceLocation(
                    status="resolved",
                    path=str(project / "off_by_one_pagination.py"),
                    line=8,
                    function="paginate",
                ),
            )
        ],
    )
    proposal = _proposal(
        unit.identifier,
        "--- a/off_by_one_pagination.py\n"
        "+++ b/off_by_one_pagination.py\n"
        "@@ -6,3 +6,3 @@\n"
        "     start = page * page_size\n"
        "-    stop = start + page_size - 1\n"
        "+    stop = start + page_size\n"
        "     return items[start:stop]\n",
        target_file="off_by_one_pagination.py",
    )

    report = RepairWorker(
        FixtureAgentAdapter([proposal]), max_attempts=1, mutation_test=True, max_mutants=5
    ).repair(project, unit)

    assert report.final_status.value == "ACCEPTED"
    assert report.targeted_test_passed
    assert report.test_integrity_passed
    assert report.verification_report is not None and report.verification_report.regression_passed
    assert report.mutation_report is not None
    assert report.mutation_report.total_generated <= 5
    assert report.mutation_report.total_generated > 0
    assert report.mutation_report.killed + report.mutation_report.survived + report.mutation_report.inconclusive == report.mutation_report.total_generated
    assert (project / "off_by_one_pagination.py").read_text(encoding="utf-8") == original_source
