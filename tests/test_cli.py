from pathlib import Path
import json

from faultline.cli import main


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
    assert report["final_status"] == "PROVISIONALLY_SUCCESSFUL"
    assert "PROVISIONALLY_SUCCESSFUL" in capsys.readouterr().out
