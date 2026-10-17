from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor

from faultline.agent_adapter import build_repair_request
from faultline.cli import _batch_adapter, main
from faultline.models import FailureRecord, SourceLocation, WorkUnit


def test_scan_returns_success_and_writes_structured_report(tmp_path, capsys):
    (tmp_path / "test_failing.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    output_path = tmp_path / "report.json"

    result = main(["scan", str(tmp_path), "--json-output", str(output_path)])

    assert result == 0
    assert "1 failure(s)" in capsys.readouterr().out
    assert '"failures"' in output_path.read_text(encoding="utf-8")


def test_scan_empty_suite_is_successful(tmp_path, capsys):
    result = main(["scan", str(tmp_path)])

    assert result == 0
    assert "0 test(s)" in capsys.readouterr().out


def test_repair_invalid_project_returns_meaningful_error(tmp_path, capsys):
    result = main(["repair", str(tmp_path / "missing")])

    assert result == 2
    assert "Collection error" in capsys.readouterr().out


def test_repair_import_runs_verified_fixture_proposal_and_writes_json(tmp_path, capsys):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() == 2\n", encoding="utf-8"
    )
    request = main(["scan", str(tmp_path), "--json-output", str(tmp_path / "scan.json")])
    assert request == 0
    scan = json.loads((tmp_path / "scan.json").read_text(encoding="utf-8"))
    work_unit_id = scan["work_units"][0]["work_unit_id"]
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "work_unit_id": work_unit_id,
                "target_file": "app.py",
                "root_cause_explanation": "off by one",
                "unified_diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
                "attempt_number": 1,
                "execution_mode": "offline_fixture",
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "repair.json"

    result = main(
        [
            "repair-import",
            str(tmp_path),
            "--proposal",
            str(proposal),
            "--json-output",
            str(report_path),
            "--max-attempts",
            "1",
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "ACCEPTED"
    assert report["acceptance_decision"]["accepted"] is True
    assert "ACCEPTED" in capsys.readouterr().out


def test_repair_batch_matches_fixture_proposals_by_work_unit_id(tmp_path):
    proposals = tmp_path / "proposals"
    proposals.mkdir()
    # Deliberately sort the files opposite to the WorkUnit input order. A
    # shared positional queue would give each worker the wrong patch.
    for filename, name, expected in (
        ("01-unit-b.json", "b", 3),
        ("02-unit-a.json", "a", 2),
    ):
        (proposals / filename).write_text(
            json.dumps(
                {
                    "work_unit_id": f"unit-{name}",
                    "target_file": f"app_{name}.py",
                    "root_cause_explanation": "off by one",
                    "unified_diff": (
                        f"--- a/app_{name}.py\n+++ b/app_{name}.py\n@@ -1,2 +1,2 @@\n"
                        " def value():\n"
                        f"-    return 1\n+    return {expected}\n"
                    ),
                    "attempt_number": 1,
                    "execution_mode": "offline_fixture",
                }
            ),
            encoding="utf-8",
        )

    adapter = _batch_adapter("fixture", proposals)
    requests = [
        build_repair_request(
            WorkUnit(
                work_unit_id=f"unit-{name}",
                source_file=f"app_{name}.py",
                failures=[
                    FailureRecord(
                        nodeid=f"test_{name}.py::test_value",
                        traceback="Traceback",
                        message="AssertionError: expected",
                        source_location=SourceLocation(
                            status="resolved", path=f"app_{name}.py", line=2, function="value"
                        ),
                    )
                ],
            ),
            source_snippet="def value():\n    return 1",
            authorized_paths=[f"app_{name}.py"],
        )
        for name in ("a", "b")
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(adapter.generate_patch, requests))

    assert [proposal.target_file for proposal in results] == ["app_a.py", "app_b.py"]


def test_repair_batch_rejects_invalid_backend_with_proposal_directory(tmp_path, capsys):
    input_path = tmp_path / "units.json"
    input_path.write_text("[]", encoding="utf-8")
    proposals = tmp_path / "proposals"
    proposals.mkdir()

    result = main(
        [
            "repair-batch",
            str(tmp_path),
            "--input",
            str(input_path),
            "--backend",
            "not-a-backend",
            "--proposal-input",
            str(proposals),
        ]
    )

    assert result == 2
    assert "backend must be one of" in capsys.readouterr().out
