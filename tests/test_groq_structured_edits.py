import json
import shutil
from types import SimpleNamespace

import pytest

from faultline.groq_provider import GroqInvalidProposalError, GroqRepairAgent
from faultline.models import ExecutionMode, FailureRecord, PatchProposal, RepairRequest, SourceLocation, WorkUnit
from faultline.patcher import validate_and_apply_patch
from faultline.worker import RepairWorker


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(payload):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
    )


def _request(
    *,
    target_file: str = "app.py",
    source_snippet: str = "def value():\n    return 1\n",
    source_line_start: int = 1,
    authorized_paths: list[str] | None = None,
) -> RepairRequest:
    return RepairRequest(
        work_unit_id="unit-1",
        target_file=target_file,
        target_function="value",
        source_snippet=source_snippet,
        source_line_start=source_line_start,
        traceback="AssertionError: expected 2",
        failing_test_node_id="tests/test_app.py::test_value",
        exception_class="AssertionError",
        error_message="expected 2",
        authorized_paths=authorized_paths or [target_file],
        repair_constraints=["Do not modify tests."],
    )


def _structured_payload(
    *,
    target_file: str = "app.py",
    original_text: str = "    return 1",
    replacement_text: str = "    return 2",
    **edit_fields,
):
    return {
        "target_file": target_file,
        "root_cause_explanation": "The implementation returns the stale value.",
        "edit": {
            "original_text": original_text,
            "replacement_text": replacement_text,
            **edit_fields,
        },
    }


def test_missing_hunk_line_numbers_are_rejected_before_worker_validation():
    request = _request()
    payload = {
        "target_file": "app.py",
        "root_cause_explanation": "The implementation returns the stale value.",
        "unified_diff": "--- a/app.py\n+++ b/app.py\n@@\n-    return 1\n+    return 2\n",
    }

    with pytest.raises(GroqInvalidProposalError, match="invalid hunk header"):
        GroqRepairAgent(client=_FakeClient([_response(payload)])).generate_patch(request)


def test_incorrect_new_line_ranges_are_rejected_by_patch_validator(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    proposal = PatchProposal(
        work_unit_id="unit-1",
        target_file="app.py",
        root_cause_explanation="wrong range",
        unified_diff=(
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +99,2 @@\n"
            " def value():\n-    return 1\n+    return 2\n"
        ),
        attempt_number=1,
        execution_mode=ExecutionMode.GROQ_AUTOMATED,
    )

    result = validate_and_apply_patch(proposal, tmp_path, authorized_paths=["app.py"])

    assert not result.valid
    assert "new line range" in (result.reason or "")


def test_structured_edit_generates_valid_deterministic_diff_with_line_numbers(tmp_path):
    source = "def value():\n    return 1\n"
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")
    response = _response(_structured_payload(start_line=2, end_line=2))
    client = _FakeClient([response])

    proposal = GroqRepairAgent(client=client, source_root=tmp_path).generate_patch(_request())

    assert "@@ -1,2 +1,2 @@" in proposal.unified_diff
    assert "\n@@\n" not in proposal.unified_diff
    assert "-    return 1\n+    return 2" in proposal.unified_diff
    assert proposal.diagnostic_notes and "deterministically" in proposal.diagnostic_notes
    prompt = client.completions.calls[0]["messages"][1]["content"]
    assert "0001 | def value():" in prompt
    assert "0002 |     return 1" in prompt


def test_missing_source_match_is_rejected_against_authorized_file(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    response = _response(_structured_payload(original_text="    return 99"))

    with pytest.raises(GroqInvalidProposalError, match="not found exactly"):
        GroqRepairAgent(client=_FakeClient([response]), source_root=tmp_path).generate_patch(_request())


def test_ambiguous_source_match_is_rejected(tmp_path):
    source = "def first():\n    return 1\n\ndef second():\n    return 1\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    response = _response(_structured_payload())
    request = _request(source_snippet=source)

    with pytest.raises(GroqInvalidProposalError, match="multiple locations"):
        GroqRepairAgent(client=_FakeClient([response]), source_root=tmp_path).generate_patch(request)


def test_unauthorized_file_path_is_rejected(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    response = _response(_structured_payload(target_file="other.py"))

    with pytest.raises(GroqInvalidProposalError, match="does not match the request"):
        GroqRepairAgent(client=_FakeClient([response]), source_root=tmp_path).generate_patch(_request())


def test_valid_structured_patch_is_accepted_and_original_project_is_preserved(tmp_path):
    source = "def value():\n    return 1\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    response = _response(_structured_payload(start_line=2, end_line=2))
    proposal = GroqRepairAgent(
        client=_FakeClient([response]),
        source_root=tmp_path,
    ).generate_patch(_request())
    candidate = tmp_path / "candidate"
    shutil.copytree(tmp_path, candidate, ignore=shutil.ignore_patterns("candidate"))

    result = validate_and_apply_patch(proposal, candidate, authorized_paths=["app.py"])

    assert result.valid
    assert (candidate / "app.py").read_text(encoding="utf-8") == "def value():\n    return 2\n"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == source


def test_invalid_diff_feedback_reaches_bounded_retry_and_can_then_accept(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    unit = WorkUnit(
        work_unit_id="unit-1",
        source_file=str(tmp_path / "app.py"),
        target_function="value",
        test_node_ids=["test_app.py::test_value"],
        failures=[
            FailureRecord(
                nodeid="test_app.py::test_value",
                traceback="AssertionError",
                message="AssertionError: expected 2",
                source_location=SourceLocation(
                    status="resolved",
                    path=str(tmp_path / "app.py"),
                    line=2,
                    function="value",
                ),
            )
        ],
    )
    first = {
        "target_file": "app.py",
        "root_cause_explanation": "The hunk is intentionally malformed for this retry test.",
        "unified_diff": "--- a/app.py\n+++ b/app.py\n@@\n-    return 1\n+    return 2\n",
    }
    client = _FakeClient([
        _response(first),
        _response(_structured_payload(start_line=2, end_line=2)),
    ])
    agent = GroqRepairAgent(client=client, source_root=tmp_path)

    report = RepairWorker(agent, max_attempts=2).repair(tmp_path, unit)

    assert report.final_status.value == "ACCEPTED"
    assert len(report.attempts) == 2
    assert report.attempts[0].failure_reason == "invalid_proposal"
    assert "invalid hunk header" in client.completions.calls[1]["messages"][1]["content"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"
