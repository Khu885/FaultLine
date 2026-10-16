"""Provider-independent repair adapters.

The desktop OmniRush app does not expose a supported synchronous repair SDK to
this repository.  The OmniRush adapter therefore performs an explicit
request-export/proposal-import handoff.  The fixture adapter is only for
offline tests and is never selected implicitly by the live adapter.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .models import (
    ExecutionMode,
    PatchProposal,
    RepairRequest,
    WorkUnit,
)


class AgentUnavailableError(RuntimeError):
    """Raised when the selected agent cannot produce a proposal."""


class InvalidAgentProposalError(ValueError):
    """Raised when an agent response is not a valid PatchProposal."""


class RepairAgent(Protocol):
    """Minimal interface required by the repair worker."""

    execution_mode: ExecutionMode

    def generate_patch(
        self,
        request: RepairRequest,
        previous_feedback: str | None = None,
    ) -> PatchProposal:
        """Return one candidate proposal for the request."""


def build_repair_request(
    work_unit: WorkUnit,
    *,
    source_snippet: str,
    authorized_paths: Sequence[str],
    previous_feedback: str | None = None,
) -> RepairRequest:
    """Build the stable, constrained context shared by all adapters."""
    failure = work_unit.failures[0] if work_unit.failures else None
    if failure is None:
        raise ValueError("cannot build a repair request for an empty WorkUnit")
    location = failure.source_location
    exception_match = re.findall(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b", failure.message)
    return RepairRequest(
        work_unit_id=work_unit.identifier,
        target_file=work_unit.source_file or (location.path if location else ""),
        target_function=work_unit.target_function or (location.function if location else None),
        source_snippet=source_snippet,
        traceback=failure.traceback,
        failing_test_node_id=work_unit.failing_test_node_id or failure.nodeid,
        exception_class=exception_match[-1] if exception_match else None,
        error_message=failure.message,
        previous_feedback=previous_feedback,
        authorized_paths=list(authorized_paths),
        repair_constraints=[
            "Fix the underlying application-source defect.",
            "Do not modify tests or protected configuration files.",
            "Do not delete assertions, skip tests, or suppress failures.",
            "Prefer the smallest behaviorally correct change.",
            "Return a root-cause explanation and a unified diff.",
            "Do not claim the patch works without runtime verification.",
        ],
    )


class FixtureAgentAdapter:
    """Deterministic proposal source for offline tests.

    Responses are supplied by the caller, making the fixture explicit and
    preventing this adapter from pretending to be an AI provider.
    """

    execution_mode = ExecutionMode.FIXTURE

    def __init__(
        self,
        proposals: Sequence[PatchProposal] | None = None,
        *,
        generator: Callable[[RepairRequest, str | None], PatchProposal] | None = None,
        proposals_by_work_unit: Mapping[str, Sequence[PatchProposal]] | None = None,
    ) -> None:
        self._proposals = list(proposals or [])
        self._proposals_by_work_unit = {
            work_unit_id: list(unit_proposals)
            for work_unit_id, unit_proposals in (proposals_by_work_unit or {}).items()
        }
        self._generator = generator
        self.requests: list[RepairRequest] = []
        self._lock = threading.Lock()

    def generate_patch(
        self,
        request: RepairRequest,
        previous_feedback: str | None = None,
    ) -> PatchProposal:
        with self._lock:
            self.requests.append(request.model_copy(update={"previous_feedback": previous_feedback}))
        if self._generator is not None:
            proposal = self._generator(request, previous_feedback)
        elif self._proposals_by_work_unit:
            with self._lock:
                proposals = self._proposals_by_work_unit.get(request.work_unit_id)
                if not proposals:
                    raise AgentUnavailableError(
                        f"offline fixture has no proposal for WorkUnit {request.work_unit_id}"
                    )
                proposal = proposals.pop(0)
        elif self._proposals:
            with self._lock:
                if not self._proposals:
                    raise AgentUnavailableError("offline fixture has no proposal for this attempt")
                proposal = self._proposals.pop(0)
        else:
            raise AgentUnavailableError("offline fixture has no proposal for this attempt")
        if not isinstance(proposal, PatchProposal):
            raise InvalidAgentProposalError("fixture response is not a PatchProposal")
        return proposal.model_copy(
            update={
                "work_unit_id": request.work_unit_id,
                "execution_mode": ExecutionMode.FIXTURE,
            }
        )


class OmniRushHumanMediatedAdapter:
    """Explicit OmniRush request export and proposal import adapter."""

    execution_mode = ExecutionMode.OMNIRUSH_HUMAN_MEDIATED

    def __init__(
        self,
        *,
        request_output: str | Path | None = None,
        proposal_input: str | Path | None = None,
    ) -> None:
        self.request_output = Path(request_output) if request_output else None
        self.proposal_input = Path(proposal_input) if proposal_input else None

    def generate_patch(
        self,
        request: RepairRequest,
        previous_feedback: str | None = None,
    ) -> PatchProposal:
        request_to_export = request.model_copy(update={"previous_feedback": previous_feedback})
        if self.request_output is not None:
            self.request_output.parent.mkdir(parents=True, exist_ok=True)
            self.request_output.write_text(
                json.dumps(request_to_export.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
        if self.proposal_input is None:
            destination = str(self.request_output) if self.request_output else "an export file"
            raise AgentUnavailableError(
                "OmniRush execution is human-mediated; send the exported request "
                f"to an actual OmniRush session and import its proposal from a file ({destination})."
            )
        try:
            payload = json.loads(self.proposal_input.read_text(encoding="utf-8"))
            proposal = PatchProposal.model_validate(payload)
        except FileNotFoundError as exc:
            raise AgentUnavailableError(f"proposal file does not exist: {self.proposal_input}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise InvalidAgentProposalError(f"proposal file is invalid: {exc}") from exc
        return proposal.model_copy(
            update={
                "work_unit_id": request.work_unit_id,
                "execution_mode": ExecutionMode.OMNIRUSH_HUMAN_MEDIATED,
            }
        )


class UnavailableAgentAdapter:
    """Explicit provider-unavailable adapter for error-path tests."""

    execution_mode = ExecutionMode.UNAVAILABLE

    def generate_patch(
        self,
        request: RepairRequest,
        previous_feedback: str | None = None,
    ) -> PatchProposal:
        raise AgentUnavailableError("no repair agent provider is configured")


def load_proposal(path: str | Path) -> PatchProposal:
    """Validate a proposal JSON file for explicit import workflows."""
    try:
        return PatchProposal.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentUnavailableError(f"proposal file does not exist: {path}") from exc
    except (OSError, UnicodeError, ValidationError, ValueError, TypeError) as exc:
        raise InvalidAgentProposalError(f"proposal file is invalid: {exc}") from exc
