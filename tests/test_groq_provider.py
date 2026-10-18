import json
from types import SimpleNamespace

import pytest

from faultline.groq_provider import (
    DEFAULT_GROQ_MODEL,
    GroqInvalidProposalError,
    GroqProviderUnavailableError,
    GroqRepairAgent,
)
from faultline.models import ExecutionMode, RepairRequest


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


def _request() -> RepairRequest:
    return RepairRequest(
        work_unit_id="unit-1",
        target_file="app.py",
        target_function="value",
        source_snippet="def value():\n    return 1\n",
        traceback="AssertionError: expected 2",
        failing_test_node_id="tests/test_app.py::test_value",
        exception_class="AssertionError",
        error_message="expected 2",
        authorized_paths=["app.py"],
        repair_constraints=["Do not modify tests."],
    )


def _response(**updates):
    payload = {
        "target_file": "app.py",
        "root_cause_explanation": "The function returns the stale value.",
        "unified_diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    }
    payload.update(updates)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def test_groq_provider_parses_proposal_and_uses_json_mode():
    client = _FakeClient([_response()])

    result = GroqRepairAgent(client=client).generate_patch(_request())

    assert result.execution_mode is ExecutionMode.GROQ_AUTOMATED
    assert result.work_unit_id == "unit-1"
    assert result.attempt_number == 1
    assert client.completions.calls[0]["model"] == DEFAULT_GROQ_MODEL
    assert client.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_groq_provider_rejects_malformed_json_without_returning_raw_content():
    secret = "do-not-echo-this"
    client = _FakeClient([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=f"{{bad {secret}"))])
    ])

    with pytest.raises(GroqInvalidProposalError, match="valid JSON") as error:
        GroqRepairAgent(client=client).generate_patch(_request())

    assert secret not in str(error.value)


def test_groq_provider_requires_api_key_without_an_injected_client(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(GroqProviderUnavailableError, match="GROQ_API_KEY"):
        GroqRepairAgent().generate_patch(_request())


def test_groq_provider_does_not_echo_key_on_authentication_failure():
    secret = "gsk-secret-value"

    class AuthenticationError(Exception):
        pass

    client = _FakeClient([AuthenticationError(f"invalid key: {secret}")])

    with pytest.raises(GroqProviderUnavailableError) as error:
        GroqRepairAgent(client=client).generate_patch(_request())

    assert secret not in str(error.value)


def test_groq_provider_retries_rate_limit_and_timeout_with_bounded_backoff():
    sleeps = []

    class FakeRateLimitError(Exception):
        pass

    client = _FakeClient([FakeRateLimitError("rate limit"), TimeoutError("timed out"), _response()])
    result = GroqRepairAgent(
        client=client,
        max_retries=2,
        backoff_seconds=0.25,
        max_backoff_seconds=0.3,
        sleep=sleeps.append,
    ).generate_patch(_request())

    assert result.execution_mode is ExecutionMode.GROQ_AUTOMATED
    assert len(client.completions.calls) == 3
    assert sleeps == [0.25, 0.3]


def test_groq_provider_falls_back_when_json_mode_is_unsupported():
    class NoResponseFormatCompletions:
        def __init__(self):
            self.calls = []

        def create(self, *, model, messages, temperature):
            self.calls.append({"model": model, "messages": messages, "temperature": temperature})
            return _response()

    completions = NoResponseFormatCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = GroqRepairAgent(client=client).generate_patch(_request())

    assert result.execution_mode is ExecutionMode.GROQ_AUTOMATED
    assert len(completions.calls) == 1


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"unified_diff": ""}, "unified_diff"),
        ({"unexpected": "field"}, "validation"),
        ({"target_file": "other.py"}, "target_file"),
    ],
)
def test_groq_provider_rejects_invalid_proposals(updates, message):
    client = _FakeClient([_response(**updates)])

    with pytest.raises(GroqInvalidProposalError, match=message):
        GroqRepairAgent(client=client).generate_patch(_request())
