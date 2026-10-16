import json

import pytest

from faultline.agent_adapter import (
    AgentUnavailableError,
    FixtureAgentAdapter,
    OmniRushHumanMediatedAdapter,
    build_repair_request,
)
from faultline.integrity import snapshot_test_files, verify_test_integrity
from faultline.models import ExecutionMode, FailureRecord, PatchProposal, SourceLocation, WorkUnit
from faultline.patcher import validate_and_apply_patch


def _unit(source_file: str = "app.py") -> WorkUnit:
    failure = FailureRecord(
        nodeid="tests/test_app.py::test_value",
        traceback="Traceback",
        message="AssertionError: expected",
        source_location=SourceLocation(status="resolved", path=source_file, line=2, function="value"),
    )
    return WorkUnit(work_unit_id="unit-1", source_file=source_file, target_function="value", failures=[failure])


def _proposal(
    diff: str,
    *,
    mode: ExecutionMode = ExecutionMode.FIXTURE,
    target_file: str = "app.py",
) -> PatchProposal:
    return PatchProposal(
        work_unit_id="unit-1",
        target_file=target_file,
        root_cause_explanation="The value was off by one.",
        unified_diff=diff,
        attempt_number=1,
        execution_mode=mode,
    )


def test_fixture_adapter_returns_structured_response_and_marks_mode():
    proposal = _proposal("--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-def value():\n+def value():\n")
    request = build_repair_request(_unit(), source_snippet="def value():", authorized_paths=["app.py"])

    result = FixtureAgentAdapter([proposal]).generate_patch(request)

    assert result.execution_mode is ExecutionMode.FIXTURE
    assert result.work_unit_id == "unit-1"


def test_human_mediated_adapter_exports_request_and_requires_import(tmp_path):
    request_path = tmp_path / "request.json"
    request = build_repair_request(_unit(), source_snippet="def value():", authorized_paths=["app.py"])

    with pytest.raises(AgentUnavailableError, match="human-mediated"):
        OmniRushHumanMediatedAdapter(request_output=request_path).generate_patch(request)

    exported = json.loads(request_path.read_text(encoding="utf-8"))
    assert exported["work_unit_id"] == "unit-1"
    assert any("Do not modify tests" in item for item in exported["repair_constraints"])


def test_empty_malformed_and_unauthorized_patches_are_rejected(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    base = dict(
        work_unit_id="unit-1",
        target_file="app.py",
        root_cause_explanation="x",
        attempt_number=1,
        execution_mode=ExecutionMode.FIXTURE,
    )
    for diff, expected in (("", "empty patch"), ("not a diff", "missing ---")):
        result = validate_and_apply_patch(
            PatchProposal(unified_diff=diff, **base), tmp_path, authorized_paths=["app.py"]
        )
        assert not result.valid
        assert expected in (result.reason or "")

    unauthorized = PatchProposal(
        unified_diff="--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-x\n+y\n", **base
    )
    result = validate_and_apply_patch(unauthorized, tmp_path, authorized_paths=["app.py"])
    assert not result.valid
    assert "outside" in (result.reason or "")


def test_test_and_traversal_changes_are_rejected(tmp_path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    test_patch = _proposal(
        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1,1 +1,1 @@\n-assert 1\n+assert 2\n",
        target_file="tests/test_app.py",
    )
    result = validate_and_apply_patch(test_patch, tmp_path, authorized_paths=["tests/test_app.py"])
    assert not result.valid
    assert "test-file" in (result.reason or "")

    traversal = _proposal(
        "--- a/../app.py\n+++ b/../app.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    result = validate_and_apply_patch(traversal, tmp_path, authorized_paths=["app.py"])
    assert not result.valid
    assert "traversal" in (result.reason or "")


def test_syntax_error_and_valid_application_are_reported(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    syntax = _proposal(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return (\n"
    )
    result = validate_and_apply_patch(syntax, tmp_path, authorized_paths=["app.py"])
    assert not result.valid
    assert "syntax" in (result.reason or "")

    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    valid = _proposal(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"
    )
    result = validate_and_apply_patch(valid, tmp_path, authorized_paths=["app.py"])
    assert result.valid
    assert path.read_text(encoding="utf-8") == "def value():\n    return 2\n"


def test_suppression_and_failed_context_are_rejected(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    suppression = _proposal(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return pytest.skip('no')\n"
    )
    result = validate_and_apply_patch(suppression, tmp_path, authorized_paths=["app.py"])
    assert not result.valid
    assert "suppression" in (result.reason or "")

    bad_context = _proposal(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def other():\n-    return 1\n+    return 2\n"
    )
    result = validate_and_apply_patch(bad_context, tmp_path, authorized_paths=["app.py"])
    assert not result.valid
    assert "context" in (result.reason or "")


def test_symlink_destination_is_rejected_when_supported(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")
    proposal = _proposal(
        "--- a/linked.py\n+++ b/linked.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
        target_file="linked.py",
    )

    result = validate_and_apply_patch(proposal, tmp_path, authorized_paths=["linked.py"])

    assert not result.valid
    assert "symbolic-link" in (result.reason or "")


def test_test_integrity_detects_assertion_modification_and_new_files(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_path = tests / "test_app.py"
    test_path.write_text("def test_value():\n    assert 1 == 1\n", encoding="utf-8")
    before = snapshot_test_files(tmp_path)
    test_path.write_text("def test_value():\n    assert 1 == 2\n", encoding="utf-8")
    (tests / "test_added.py").write_text("def test_added(): pass\n", encoding="utf-8")

    result = verify_test_integrity(tmp_path, before)

    assert not result.passed
    assert "tests/test_app.py" in result.changed_paths
    assert "tests/test_added.py" in result.added_paths
    assert "integrity violation" in (result.reason or "")
