import json
from pathlib import Path

from faultline.cli import main
from faultline.models import ExecutionMode, PatchProposal


VALID_DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"


class _FakeGroqAgent:
    execution_mode = ExecutionMode.GROQ_AUTOMATED

    def __init__(self, **_kwargs):
        self.requests = []

    def generate_patch(self, request, previous_feedback=None):
        self.requests.append((request, previous_feedback))
        return PatchProposal(
            work_unit_id=request.work_unit_id,
            target_file=request.target_file,
            root_cause_explanation="The implementation returned the stale value.",
            unified_diff=VALID_DIFF,
            attempt_number=1,
            execution_mode=ExecutionMode.GROQ_AUTOMATED,
        )


class _StaticGroqAgent:
    execution_mode = ExecutionMode.GROQ_AUTOMATED

    def __init__(self, proposal, **_kwargs):
        self.proposal = proposal

    def generate_patch(self, request, previous_feedback=None):
        return self.proposal.model_copy(update={"work_unit_id": request.work_unit_id})


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    return tmp_path


def test_repair_ai_proposal_only_writes_request_proposal_and_envelope(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    monkeypatch.setattr("faultline.cli.GroqRepairAgent", _FakeGroqAgent)
    request_path = project / "request.json"
    proposal_path = project / "proposal.json"
    report_path = project / "ai-report.json"

    result = main(
        [
            "repair-ai",
            str(project),
            "--proposal-only",
            "--request-output",
            str(request_path),
            "--proposal-output",
            str(proposal_path),
            "--json-output",
            str(report_path),
        ]
    )

    assert result == 0
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    envelope = json.loads(report_path.read_text(encoding="utf-8"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert proposal["execution_mode"] == "groq_automated"
    assert proposal["target_file"] == "app.py"
    assert envelope["mode"] == "proposal_only"
    assert envelope["provider"] == "groq"
    assert envelope["proposal"] == proposal
    assert request["authorized_paths"] == ["app.py"]
    assert (project / "app.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"


def test_repair_ai_verified_mode_uses_existing_worker_and_preserves_original(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    monkeypatch.setattr("faultline.cli.GroqRepairAgent", _FakeGroqAgent)
    report_path = project / "repair-report.json"
    proposal_path = project / "proposal.json"
    original = (project / "app.py").read_text(encoding="utf-8")

    result = main(
        [
            "repair-ai",
            str(project),
            "--json-output",
            str(report_path),
            "--proposal-output",
            str(proposal_path),
            "--max-attempts",
            "1",
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "ACCEPTED"
    assert report["execution_mode"] == "groq_automated"
    assert report["acceptance_decision"]["accepted"] is True
    assert report["targeted_test_passed"] is True
    assert report["test_integrity_passed"] is True
    assert report["verification_report"]["regression_passed"] is True
    assert proposal["execution_mode"] == "groq_automated"
    assert (project / "app.py").read_text(encoding="utf-8") == original
    assert "\x1b[" not in capsys.readouterr().out
    assert "\x1b[" not in report_path.read_text(encoding="utf-8")


def test_repair_ai_requires_api_key_before_provider_work(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = main(["repair-ai", str(project), "--proposal-only"])

    assert result == 2
    assert "GROQ_API_KEY" in capsys.readouterr().out


def test_repair_ai_rejects_unknown_provider_without_scanning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")

    result = main(["repair-ai", str(tmp_path), "--provider", "unknown"])

    assert result == 2
    assert "provider must be 'groq'" in capsys.readouterr().out


def test_repair_ai_rejects_unauthorized_target_without_touching_source(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    unauthorized = PatchProposal(
        work_unit_id="ignored",
        target_file="other.py",
        root_cause_explanation="wrong target",
        unified_diff="--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-x\n+y\n",
        attempt_number=1,
        execution_mode=ExecutionMode.GROQ_AUTOMATED,
    )
    monkeypatch.setattr(
        "faultline.cli.GroqRepairAgent",
        lambda **kwargs: _StaticGroqAgent(unauthorized, **kwargs),
    )
    report_path = project / "rejected.json"
    original = (project / "app.py").read_text(encoding="utf-8")

    result = main(
        ["repair-ai", str(project), "--max-attempts", "1", "--json-output", str(report_path)]
    )

    assert result == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "REJECTED"
    assert report["attempts"][0]["failure_reason"] == "patch_rejected"
    assert (project / "app.py").read_text(encoding="utf-8") == original


def test_repair_ai_rejects_new_regression_after_targeted_pass(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\n"
        "def test_target():\n    assert value() == 2\n\n"
        "def test_other():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    proposal = PatchProposal(
        work_unit_id="ignored",
        target_file="app.py",
        root_cause_explanation="The target expects the corrected value.",
        unified_diff=VALID_DIFF,
        attempt_number=1,
        execution_mode=ExecutionMode.GROQ_AUTOMATED,
    )
    monkeypatch.setattr(
        "faultline.cli.GroqRepairAgent",
        lambda **kwargs: _StaticGroqAgent(proposal, **kwargs),
    )
    report_path = tmp_path / "rejected-regression.json"

    result = main(
        ["repair-ai", str(tmp_path), "--max-attempts", "1", "--json-output", str(report_path)]
    )

    assert result == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "REJECTED"
    assert report["targeted_test_passed"] is True
    assert report["verification_report"]["newly_failing"] == ["test_app.py::test_other"]
