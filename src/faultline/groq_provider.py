"""Groq-backed automated repair proposal provider.

The provider deliberately keeps the Groq SDK behind a small protocol so the
repair flow can be tested without network access.  A real client is created
only when ``generate_patch`` is called and reads its key from ``GROQ_API_KEY``
unless an explicit key or test client is supplied.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from difflib import unified_diff
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, Protocol

from pydantic import ValidationError

from .agent_adapter import AgentUnavailableError, InvalidAgentProposalError
from .models import ExecutionMode, PatchProposal, RepairRequest

try:  # Keep injected-client tests usable even before dependencies are installed.
    from groq import Groq
except ImportError:  # pragma: no cover - exercised only in an incomplete install.
    Groq = None  # type: ignore[assignment,misc]


DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
"""Default Groq model; callers can select another deployed Groq model."""

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_BACKOFF_SECONDS = 8.0
MAX_ALLOWED_RETRIES = 5

# Context is intentionally field-by-field bounded before it reaches the model.
_MAX_WORK_UNIT_ID = 200
_MAX_PATH = 500
_MAX_FUNCTION = 500
_MAX_NODE_ID = 500
_MAX_EXCEPTION = 200
_MAX_ERROR_MESSAGE = 1_500
_MAX_SOURCE_SNIPPET = 3_500
_MAX_TRACEBACK = 3_500
_MAX_FEEDBACK = 2_500
_MAX_LIST_ITEMS = 16
_MAX_AUTHORIZED_PATH = 300
_MAX_CONSTRAINT = 500
_MAX_CONTEXT_CHARS = 16_000
_MAX_LINE_NUMBER = 1_000_000

_DIFF_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)


class GroqCompletions(Protocol):
    """Small seam for ``client.chat.completions`` used by the provider."""

    def create(self, **kwargs: Any) -> Any:
        """Create one chat completion."""


class GroqChat(Protocol):
    completions: GroqCompletions


class GroqClient(Protocol):
    """Minimum client surface used by :class:`GroqRepairAgent`."""

    chat: GroqChat


# Explicit aliases make the dependency-injection seam discoverable to callers
# without tying them to the SDK's generated client types.
GroqClientProtocol = GroqClient
SleepFunction = Callable[[float], None]
SourceReader = Callable[[str], str]


class GroqProviderUnavailableError(AgentUnavailableError):
    """Raised when Groq cannot be reached or is not configured."""


class GroqInvalidProposalError(InvalidAgentProposalError):
    """Raised when Groq returns a response that is not a safe proposal."""


class _ResponseFormatUnsupported(Exception):
    """Internal signal that a client/API does not support JSON mode."""


class GroqRepairAgent:
    """Generate strictly validated repair proposals with the official Groq SDK.

    ``max_retries`` is the number of retries after the first request, capped by
    ``MAX_ALLOWED_RETRIES``.  Retry delays are deterministic exponential
    backoff, capped by ``max_backoff_seconds``; injecting ``sleep`` keeps this
    behavior deterministic and offline-testable.
    """

    execution_mode = ExecutionMode.GROQ_AUTOMATED

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_GROQ_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        client: GroqClient | None = None,
        sleep: SleepFunction = time.sleep,
        environ: Mapping[str, str] | None = None,
        source_root: str | Path | None = None,
        source_reader: SourceReader | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_ALLOWED_RETRIES:
            raise ValueError(f"max_retries must be an integer between 0 and {MAX_ALLOWED_RETRIES}")
        if (
            not isinstance(backoff_seconds, (int, float))
            or isinstance(backoff_seconds, bool)
            or not math.isfinite(backoff_seconds)
            or backoff_seconds < 0
        ):
            raise ValueError("backoff_seconds must be a finite non-negative number")
        if (
            not isinstance(max_backoff_seconds, (int, float))
            or isinstance(max_backoff_seconds, bool)
            or not math.isfinite(max_backoff_seconds)
            or max_backoff_seconds < 0
        ):
            raise ValueError("max_backoff_seconds must be a finite non-negative number")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        if source_reader is not None and not callable(source_reader):
            raise TypeError("source_reader must be callable")

        self.model = model.strip()
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.backoff_seconds = float(backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._api_key = api_key
        self._environment = environ if environ is not None else os.environ
        self._client = client
        self._sleep = sleep
        self._source_root = Path(source_root).expanduser().resolve() if source_root is not None else None
        self._source_reader = source_reader
        self._json_mode_supported: bool | None = None
        self._attempts: dict[str, int] = {}
        self._attempt_lock = Lock()

    def generate_patch(
        self,
        request: RepairRequest,
        previous_feedback: str | None = None,
    ) -> PatchProposal:
        """Return one Groq proposal or a safe, non-secret error."""
        if not isinstance(request, RepairRequest):
            raise TypeError("request must be a RepairRequest")

        request_for_prompt = request.model_copy(update={"previous_feedback": previous_feedback})
        attempt_number = self._next_attempt(request.work_unit_id)
        source_content, source_line_base = self._source_for_request(request_for_prompt)
        messages = self._build_messages(request_for_prompt, attempt_number)
        response = self._request_with_retries(messages)
        content = self._response_content(response)
        return self._parse_proposal(
            content,
            request_for_prompt,
            attempt_number,
            source_content=source_content,
            source_line_base=source_line_base,
        )

    def _next_attempt(self, work_unit_id: str) -> int:
        with self._attempt_lock:
            next_number = self._attempts.get(work_unit_id, 0) + 1
            self._attempts[work_unit_id] = next_number
            return next_number

    def _source_for_request(self, request: RepairRequest) -> tuple[str, int]:
        """Read one authorized source file, or use the bounded test context.

        The CLI supplies ``source_root`` so live proposals are checked against
        the current authorized file.  The bounded ``source_snippet`` fallback
        keeps direct offline provider tests and older integrations compatible;
        it is still matched exactly before a structured edit is converted.
        """
        target = self._authorized_target(request.target_file, request.authorized_paths)
        if self._source_reader is not None:
            try:
                source = self._source_reader(target)
            except Exception as exc:
                raise GroqInvalidProposalError(
                    f"authorized source file could not be read ({type(exc).__name__})"
                ) from exc
            if not isinstance(source, str) or not source:
                raise GroqInvalidProposalError("authorized source reader returned no source text")
            return source, 1

        if self._source_root is not None:
            source_path = self._source_path(target)
            try:
                source = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise GroqInvalidProposalError(
                    f"authorized source file could not be read ({type(exc).__name__})"
                ) from exc
            if not source:
                raise GroqInvalidProposalError("authorized source file is empty")
            if request.source_snippet and request.source_snippet not in source:
                raise GroqInvalidProposalError(
                    "repair request source context no longer matches the authorized source file"
                )
            return source, 1

        if not request.source_snippet:
            raise GroqInvalidProposalError(
                "structured edit requires source text from an authorized source file"
            )
        return request.source_snippet, request.source_line_start

    @classmethod
    def _authorized_target(cls, target_file: str, authorized_paths: Sequence[str]) -> str:
        target = cls._canonical_relative_path(target_file, "target_file")
        authorized: set[str] = set()
        for path in authorized_paths:
            authorized.add(cls._canonical_relative_path(path, "authorized path"))
        if target not in authorized:
            raise GroqInvalidProposalError(f"target file is not authorized: {target}")
        return target

    @staticmethod
    def _canonical_relative_path(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GroqInvalidProposalError(f"{label} must be a non-empty relative path")
        raw = value.strip().replace("\\", "/")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or ":" in path.parts[0] or "" in path.parts:
            raise GroqInvalidProposalError(f"{label} must be an authorized relative path")
        normalized = path.as_posix()
        if normalized in {"", "."}:
            raise GroqInvalidProposalError(f"{label} must be a non-empty relative path")
        return normalized

    def _source_path(self, target: str) -> Path:
        assert self._source_root is not None
        candidate = self._source_root.joinpath(*PurePosixPath(target).parts)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise GroqInvalidProposalError("authorized source path could not be resolved") from exc
        if self._source_root not in (resolved, *resolved.parents):
            raise GroqInvalidProposalError("authorized source path escapes the project root")
        current = self._source_root
        for part in PurePosixPath(target).parts:
            current /= part
            if current.is_symlink():
                raise GroqInvalidProposalError("authorized source path is a symbolic link")
        if not resolved.is_file():
            raise GroqInvalidProposalError("authorized source path is not a regular file")
        return resolved

    def _get_client(self) -> GroqClient:
        if self._client is not None:
            return self._client
        api_key = self._api_key if self._api_key is not None else self._environment.get("GROQ_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise GroqProviderUnavailableError(
                "Groq provider is unavailable: set GROQ_API_KEY before using the automated provider"
            )
        if Groq is None:
            raise GroqProviderUnavailableError(
                "Groq provider is unavailable: the official groq package is not installed"
            )
        try:
            # The SDK-level timeout applies to every request made by this client.
            # Disable the SDK's own retry loop so the provider's bounded,
            # injectable retry policy is the only one in effect.
            self._client = Groq(api_key=api_key, timeout=self.timeout, max_retries=0)
        except Exception as exc:
            raise GroqProviderUnavailableError(
                f"Groq provider is unavailable while creating its client ({type(exc).__name__})"
            ) from exc
        return self._client

    def _request_with_retries(self, messages: list[dict[str, str]]) -> Any:
        client = self._get_client()
        try:
            create = client.chat.completions.create
        except (AttributeError, TypeError) as exc:
            raise GroqProviderUnavailableError("Groq provider client has no chat completion interface") from exc
        if not callable(create):
            raise GroqProviderUnavailableError("Groq provider client has no callable chat completion interface")

        last_error: Exception | None = None
        for retry_number in range(self.max_retries + 1):
            try:
                return self._create_completion(create, messages)
            except _ResponseFormatUnsupported:
                # _create_completion performs the unformatted fallback itself;
                # this branch is defensive if a custom client raises the signal.
                self._json_mode_supported = False
                try:
                    return self._create_completion(create, messages)
                except Exception as exc:
                    last_error = exc
            except Exception as exc:
                last_error = exc

            if last_error is None or not self._is_retryable(last_error):
                raise self._unavailable_error(last_error) from last_error
            if retry_number >= self.max_retries:
                break
            delay = min(
                self.max_backoff_seconds,
                self.backoff_seconds * (2**retry_number),
            )
            self._sleep(delay)

        assert last_error is not None  # The loop either returns or records an exception.
        raise self._unavailable_error(last_error) from last_error

    def _create_completion(self, create: Callable[..., Any], messages: list[dict[str, str]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self._json_mode_supported is not False:
            try:
                response = create(response_format={"type": "json_object"}, **kwargs)
            except TypeError as exc:
                # Older SDKs/mocks can reject the keyword before making a call.
                self._json_mode_supported = False
                return create(**kwargs)
            except Exception as exc:
                if not self._is_response_format_unsupported(exc):
                    raise
                self._json_mode_supported = False
                return create(**kwargs)
            self._json_mode_supported = True
            return response
        return create(**kwargs)

    @staticmethod
    def _is_response_format_unsupported(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if isinstance(exc, TypeError):
            return True
        if "badrequest" not in name and "invalidrequest" not in name and "unsupported" not in text:
            return False
        return "response_format" in text or "json" in text or "structured output" in text

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and (status in {408, 409, 425, 429} or status >= 500):
            return True
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        return any(
            marker in name or marker in text
            for marker in (
                "ratelimit",
                "rate_limit",
                "timeout",
                "timed out",
                "apiconnection",
                "connectionerror",
                "connecterror",
                "network",
                "serviceunavailable",
                "internalserver",
                "temporarily unavailable",
                "connection reset",
            )
        )

    @staticmethod
    def _unavailable_error(exc: Exception | None) -> GroqProviderUnavailableError:
        if exc is None:
            return GroqProviderUnavailableError("Groq provider is unavailable")
        name = type(exc).__name__
        if "auth" in name.lower() or "permission" in name.lower():
            return GroqProviderUnavailableError("Groq provider authentication failed; check GROQ_API_KEY")
        return GroqProviderUnavailableError(f"Groq provider request failed after retries ({name})")

    @staticmethod
    def _response_content(response: Any) -> str:
        choices = GroqRepairAgent._value(response, "choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            raise GroqInvalidProposalError("Groq response did not contain a completion choice")
        message = GroqRepairAgent._value(choices[0], "message")
        content = GroqRepairAgent._value(message, "content")
        if not isinstance(content, str) or not content.strip():
            raise GroqInvalidProposalError("Groq response did not contain textual JSON content")
        return content

    @staticmethod
    def _value(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    @classmethod
    def _parse_proposal(
        cls,
        content: str,
        request: RepairRequest,
        attempt_number: int,
        *,
        source_content: str,
        source_line_base: int,
    ) -> PatchProposal:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise GroqInvalidProposalError(
                f"Groq response is not valid JSON (character {exc.pos})"
            ) from exc
        if not isinstance(payload, dict):
            raise GroqInvalidProposalError("Groq response must be a JSON object")

        # These fields are provider-owned.  A model may repeat them, but it
        # cannot change the work unit, execution mode, or worker attempt.
        if "work_unit_id" in payload and payload["work_unit_id"] != request.work_unit_id:
            raise GroqInvalidProposalError("Groq proposal work_unit_id does not match the request")
        if "execution_mode" in payload and payload["execution_mode"] != ExecutionMode.GROQ_AUTOMATED.value:
            raise GroqInvalidProposalError("Groq proposal execution_mode is not GROQ_AUTOMATED")
        if "attempt_number" in payload and payload["attempt_number"] != attempt_number:
            raise GroqInvalidProposalError("Groq proposal attempt_number does not match this attempt")
        payload = dict(payload)
        payload["work_unit_id"] = request.work_unit_id
        payload["execution_mode"] = ExecutionMode.GROQ_AUTOMATED
        payload["attempt_number"] = attempt_number

        target_file = cls._authorized_target(request.target_file, request.authorized_paths)
        response_target = payload.get("target_file")
        if not isinstance(response_target, str):
            raise GroqInvalidProposalError("Groq proposal target_file is required")
        if cls._canonical_relative_path(response_target, "proposal target_file") != target_file:
            raise GroqInvalidProposalError("Groq proposal target_file does not match the request")

        structured_edit = cls._extract_structured_edit(payload)
        if structured_edit is not None:
            payload["unified_diff"] = cls._build_unified_diff_from_edit(
                structured_edit,
                source_content=source_content,
                source_line_base=source_line_base,
                target_file=target_file,
            )
            # ``edit`` is an input contract for this provider, not a second
            # PatchProposal field. The deterministic diff is the only value
            # passed through the existing proposal contract.
            for key in ("edit", "edits", "original_text", "replacement_text", "start_line", "end_line"):
                payload.pop(key, None)
            existing_notes = payload.get("diagnostic_notes")
            generated_note = "unified diff generated deterministically from one exact source replacement"
            if existing_notes:
                payload["diagnostic_notes"] = f"{existing_notes}; {generated_note}"
            else:
                payload["diagnostic_notes"] = generated_note
        else:
            unified_diff_text = payload.get("unified_diff")
            if not isinstance(unified_diff_text, str) or not unified_diff_text.strip():
                raise GroqInvalidProposalError(
                    "Groq proposal unified_diff is empty and no structured edit with exact original_text and replacement_text was supplied"
                )
            cls._validate_unified_diff_syntax(unified_diff_text, expected_target=target_file)

        try:
            proposal = PatchProposal.model_validate(payload, strict=True)
        except ValidationError as exc:
            fields = []
            for error in exc.errors():
                location = ".".join(str(part) for part in error.get("loc", ())) or "response"
                error_type = str(error.get("type", "invalid"))
                fields.append(f"{location} ({error_type})")
            detail = ", ".join(fields[:8])
            suffix = "" if len(fields) <= 8 else ", ..."
            raise GroqInvalidProposalError(
                f"Groq response failed PatchProposal validation: {detail}{suffix}"
            ) from exc

        if proposal.status != "proposed":
            raise GroqInvalidProposalError("Groq proposal status must be 'proposed'")
        if proposal.target_file != request.target_file:
            raise GroqInvalidProposalError("Groq proposal target_file does not match the request")
        if not proposal.root_cause_explanation.strip():
            raise GroqInvalidProposalError("Groq proposal root_cause_explanation must not be empty")
        if not proposal.unified_diff.strip():
            raise GroqInvalidProposalError("Groq proposal unified_diff must not be empty")
        return proposal

    @staticmethod
    def _extract_structured_edit(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        has_edit = "edit" in payload
        has_edits = "edits" in payload
        has_flat_edit = "original_text" in payload or "replacement_text" in payload
        if (has_edit and (has_edits or has_flat_edit)) or (has_edits and has_flat_edit):
            raise GroqInvalidProposalError("Groq proposal contains ambiguous edit representations")
        if has_edits:
            edits = payload.get("edits")
            if not isinstance(edits, list) or len(edits) != 1:
                raise GroqInvalidProposalError("Groq proposal must contain exactly one intended edit")
            raw_edit = edits[0]
        elif has_edit:
            raw_edit = payload.get("edit")
        elif has_flat_edit:
            raw_edit = {
                key: payload.get(key)
                for key in ("original_text", "replacement_text", "start_line", "end_line")
                if key in payload
            }
        else:
            return None
        if not isinstance(raw_edit, Mapping):
            raise GroqInvalidProposalError("Groq proposal edit must be a JSON object")
        return dict(raw_edit)

    @classmethod
    def _build_unified_diff_from_edit(
        cls,
        edit: Mapping[str, Any],
        *,
        source_content: str,
        source_line_base: int,
        target_file: str,
    ) -> str:
        unexpected_fields = set(edit) - {
            "target_file",
            "original_text",
            "replacement_text",
            "start_line",
            "end_line",
        }
        if unexpected_fields:
            names = ", ".join(sorted(str(field) for field in unexpected_fields))
            raise GroqInvalidProposalError(f"Groq proposal edit contains unsupported fields: {names}")
        if "target_file" in edit:
            edit_target = cls._canonical_relative_path(str(edit["target_file"]), "edit target_file")
            if edit_target != target_file:
                raise GroqInvalidProposalError("Groq proposal edit target_file is not authorized")
        if "original_text" not in edit or "replacement_text" not in edit:
            raise GroqInvalidProposalError(
                "Groq proposal edit requires exact original_text and replacement_text"
            )
        original_text = edit["original_text"]
        replacement_text = edit["replacement_text"]
        if not isinstance(original_text, str) or not original_text:
            raise GroqInvalidProposalError("Groq proposal original_text must be non-empty source text")
        if not isinstance(replacement_text, str):
            raise GroqInvalidProposalError("Groq proposal replacement_text must be source text")
        if original_text == replacement_text:
            raise GroqInvalidProposalError("Groq proposal edit is a no-op")

        positions: list[int] = []
        cursor = 0
        while True:
            position = source_content.find(original_text, cursor)
            if position < 0:
                break
            positions.append(position)
            cursor = position + 1
        if not positions:
            raise GroqInvalidProposalError(
                "Groq proposal original_text was not found exactly in the authorized source file"
            )
        if len(positions) != 1:
            raise GroqInvalidProposalError(
                "Groq proposal original_text matches multiple locations in the authorized source file"
            )
        position = positions[0]
        end_position = position + len(original_text)

        actual_start = source_line_base + source_content.count("\n", 0, position)
        actual_end = actual_start + original_text.count("\n") - int(original_text.endswith("\n"))
        cls._validate_edit_line(edit, "start_line", actual_start)
        cls._validate_edit_line(edit, "end_line", actual_end)

        updated_source = source_content[:position] + replacement_text + source_content[end_position:]
        diff = "".join(
            unified_diff(
                source_content.splitlines(keepends=True),
                updated_source.splitlines(keepends=True),
                fromfile=f"a/{target_file}",
                tofile=f"b/{target_file}",
                n=3,
                lineterm="\n",
            )
        )
        if not diff:
            raise GroqInvalidProposalError("Groq proposal did not produce a source change")
        cls._validate_unified_diff_syntax(diff, expected_target=target_file)
        return diff

    @staticmethod
    def _validate_edit_line(edit: Mapping[str, Any], field: str, actual: int) -> None:
        if field not in edit:
            return
        value = edit[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LINE_NUMBER:
            raise GroqInvalidProposalError(f"Groq proposal {field} must be a positive integer")
        if value != actual:
            raise GroqInvalidProposalError(
                f"Groq proposal {field} does not match the exact source text location"
            )

    @classmethod
    def _validate_unified_diff_syntax(
        cls,
        unified_diff_text: str,
        *,
        expected_target: str | None = None,
    ) -> None:
        """Reject malformed legacy diffs before they reach the patch worker."""
        lines = unified_diff_text.splitlines()
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
            raise GroqInvalidProposalError("Groq proposal unified_diff is missing standard file headers")
        try:
            old_path = lines[0][4:].strip().split("\t", 1)[0]
            new_path = lines[1][4:].strip().split("\t", 1)[0]
            if old_path.startswith("a/"):
                old_path = old_path[2:]
            if new_path.startswith("b/"):
                new_path = new_path[2:]
            old_path = cls._canonical_relative_path(old_path, "unified_diff old path")
            new_path = cls._canonical_relative_path(new_path, "unified_diff new path")
        except (IndexError, TypeError) as exc:
            raise GroqInvalidProposalError("Groq proposal unified_diff has invalid file paths") from exc
        if old_path != new_path:
            raise GroqInvalidProposalError("Groq proposal unified_diff cannot rename or create a file")
        if expected_target is not None and new_path != expected_target:
            raise GroqInvalidProposalError("Groq proposal unified_diff targets an unauthorized file")
        hunk_found = False
        index = 2
        while index < len(lines):
            match = _DIFF_HUNK_HEADER.match(lines[index])
            if match is None:
                raise GroqInvalidProposalError(
                    "Groq proposal unified_diff has an invalid hunk header; use @@ -old,count +new,count @@"
                )
            hunk_found = True
            old_count = int(match.group("old_count") or "1")
            new_count = int(match.group("new_count") or "1")
            if old_count == 0 and new_count == 0:
                raise GroqInvalidProposalError("Groq proposal unified_diff contains an empty hunk")
            index += 1
            old_seen = 0
            new_seen = 0
            while index < len(lines) and _DIFF_HUNK_HEADER.match(lines[index]) is None:
                if lines[index].startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
                    raise GroqInvalidProposalError("Groq proposal must contain one target-file diff")
                line = lines[index]
                if line == r"\ No newline at end of file":
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise GroqInvalidProposalError("Groq proposal unified_diff contains an invalid hunk line")
                if line[0] in " -":
                    old_seen += 1
                if line[0] in " +":
                    new_seen += 1
                index += 1
            if old_seen != old_count or new_seen != new_count:
                raise GroqInvalidProposalError("Groq proposal unified_diff hunk line counts do not match its header")
        if not hunk_found:
            raise GroqInvalidProposalError("Groq proposal unified_diff contains no hunk")

    @classmethod
    def _build_messages(cls, request: RepairRequest, attempt_number: int) -> list[dict[str, str]]:
        context = {
            "work_unit_id": cls._truncate(request.work_unit_id, _MAX_WORK_UNIT_ID),
            "target_file": cls._truncate(request.target_file, _MAX_PATH),
            "target_function": cls._truncate(request.target_function, _MAX_FUNCTION),
            "source_line_start": request.source_line_start,
            "source_context": cls._truncate(
                cls._line_numbered_context(request.source_snippet, request.source_line_start),
                _MAX_SOURCE_SNIPPET,
            ),
            "traceback": cls._truncate(request.traceback, _MAX_TRACEBACK),
            "failing_test_node_id": cls._truncate(request.failing_test_node_id, _MAX_NODE_ID),
            "exception_class": cls._truncate(request.exception_class, _MAX_EXCEPTION),
            "error_message": cls._truncate(request.error_message, _MAX_ERROR_MESSAGE),
            "previous_feedback": cls._truncate(request.previous_feedback, _MAX_FEEDBACK),
            "authorized_paths": [
                cls._truncate(value, _MAX_AUTHORIZED_PATH)
                for value in request.authorized_paths[:_MAX_LIST_ITEMS]
            ],
            "repair_constraints": [
                cls._truncate(value, _MAX_CONSTRAINT)
                for value in request.repair_constraints[:_MAX_LIST_ITEMS]
            ],
        }
        output_instruction = {
            "attempt_number": attempt_number,
            "required_fields": [
                "target_file",
                "root_cause_explanation",
                "edit",
            ],
            "edit_fields": {
                "original_text": "exact non-empty source text copied from source_context, without line-number labels",
                "replacement_text": "exact replacement source text; may be empty only for an intentional deletion",
                "start_line": "optional integer matching the displayed source line",
                "end_line": "optional integer matching the displayed source line",
            },
            "unified_diff": (
                "omit when edit is supplied; if a legacy diff is returned, every hunk must use "
                "@@ -old_start,old_count +new_start,new_count @@ with real line ranges, never bare @@"
            ),
            "optional_fields": ["status", "diagnostic_notes"],
            "format": "one JSON object only; no markdown fences or commentary",
        }
        context_text = cls._bounded_context_json(context)
        return [
            {
                "role": "system",
                "content": (
                    "You are a constrained FaultLine repair proposal generator. "
                    "Identify exactly one application-source edit using the exact original_text "
                    "and replacement_text from the supplied source context. FaultLine will verify "
                    "the match and generate the unified diff deterministically. Do not modify tests "
                    "or protected files. The response must be one JSON object only, with no markdown "
                    "fences. Never invent a bare @@ hunk header."
                ),
            },
            {
                "role": "user",
                "content": (
                    "RepairRequest context (bounded JSON):\n"
                    + context_text
                    + "\nProposal output contract:\n"
                    + json.dumps(output_instruction, separators=(",", ":"))
                ),
            },
        ]

    @staticmethod
    def _truncate(value: str | None, limit: int) -> str | None:
        if value is None:
            return None
        if len(value) <= limit:
            return value
        return value[:limit] + "\n...[truncated]"

    @staticmethod
    def _line_numbered_context(source_snippet: str, source_line_start: int) -> str:
        lines = source_snippet.splitlines()
        return "\n".join(
            f"{source_line_start + offset:04d} | {line}"
            for offset, line in enumerate(lines)
        )

    @classmethod
    def _bounded_context_json(cls, context: dict[str, Any]) -> str:
        """Serialize context under a fixed cap without cutting JSON in half."""
        serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= _MAX_CONTEXT_CHARS:
            return serialized

        # The ordinary field limits preserve useful detail.  If a request has
        # many long constraints/paths or lots of escaping, use a smaller,
        # still-relevant view rather than sending an unbounded prompt.
        compact = {
            "work_unit_id": cls._truncate(context.get("work_unit_id"), 100),
            "target_file": cls._truncate(context.get("target_file"), 250),
            "target_function": cls._truncate(context.get("target_function"), 250),
            "source_line_start": context.get("source_line_start"),
            "source_context": cls._truncate(context.get("source_context"), 2_000),
            "traceback": cls._truncate(context.get("traceback"), 2_000),
            "failing_test_node_id": cls._truncate(context.get("failing_test_node_id"), 250),
            "exception_class": cls._truncate(context.get("exception_class"), 100),
            "error_message": cls._truncate(context.get("error_message"), 800),
            "previous_feedback": cls._truncate(context.get("previous_feedback"), 1_000),
            "authorized_paths": list(context.get("authorized_paths", ()))[:4],
            "repair_constraints": list(context.get("repair_constraints", ()))[:4],
        }
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "DEFAULT_GROQ_MODEL",
    "GroqChat",
    "GroqClient",
    "GroqClientProtocol",
    "GroqCompletions",
    "GroqInvalidProposalError",
    "GroqProviderUnavailableError",
    "GroqRepairAgent",
    "SleepFunction",
    "SourceReader",
]
