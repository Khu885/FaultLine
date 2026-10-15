from pathlib import Path

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
