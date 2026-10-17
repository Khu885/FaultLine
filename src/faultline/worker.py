"""Single-WorkUnit repair worker with fresh-copy retry semantics."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

from .agent_adapter import (
    AgentUnavailableError,
    InvalidAgentProposalError,
    RepairAgent,
    build_repair_request,
)
from .arbiter import decide_acceptance
from .integrity import snapshot_test_files, verify_test_integrity
from .lifecycle import raise_if_cancelled
from .models import (
    FailureReason,
    MutationReport,
    PatchValidationResult,
    RepairAttempt,
    RepairReport,
    RepairState,
    TestExecutionResult,
    WorkUnit,
)
from .mutation import run_mutation_testing
from .patcher import validate_and_apply_patch
from .regression import capture_baseline, verify_candidate
from .runner import run_targeted_test


_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.py[cod]", ".pytest_cache")


class RepairWorker:
    """Process exactly one WorkUnit and never modify its original project."""

    def __init__(
        self,
        agent: RepairAgent,
        *,
        max_attempts: int = 2,
        timeout: float = 10.0,
        python_executable: str | Path | None = None,
        regression_timeout: float | None = None,
        mutation_test: bool = False,
        max_mutants: int = 5,
        mutation_timeout: float | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if regression_timeout is not None and regression_timeout <= 0:
            raise ValueError("regression_timeout must be positive")
        if isinstance(max_mutants, bool) or not isinstance(max_mutants, int) or max_mutants < 1:
            raise ValueError("max_mutants must be a positive integer")
        if mutation_timeout is not None and mutation_timeout <= 0:
            raise ValueError("mutation_timeout must be positive")
        self.agent = agent
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.python_executable = python_executable
        # Global verification is a complete suite run, so it has its own
        # bound instead of silently reusing the shorter targeted-test bound.
        self.regression_timeout = regression_timeout if regression_timeout is not None else max(timeout, 60.0)
        self.mutation_test = mutation_test
        self.max_mutants = max_mutants
        self.mutation_timeout = mutation_timeout if mutation_timeout is not None else self.regression_timeout

    def repair(
        self,
        project_root: str | Path,
        work_unit: WorkUnit,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> RepairReport:
        """Generate, validate, and execute candidates until success or exhaustion."""
        raise_if_cancelled(cancellation_event, "worker cancelled before start")
        started = time.monotonic()
        root = Path(project_root).expanduser().resolve()
        limitations = [
            "Candidate execution uses a disposable temporary copy, not an OS-level security sandbox.",
            "Network, CPU, memory, and process limits are not enforced by this local runner.",
        ]
        if not root.is_dir():
            return RepairReport(
                work_unit_id=work_unit.identifier,
                execution_mode=self.agent.execution_mode,
                final_status=RepairState.FAILED,
                total_duration_seconds=time.monotonic() - started,
                limitations=limitations + ["Project path is not a directory."],
            )
        if not work_unit.source_file or not work_unit.failing_test_node_id:
            return RepairReport(
                work_unit_id=work_unit.identifier,
                execution_mode=self.agent.execution_mode,
                final_status=RepairState.REJECTED,
                total_duration_seconds=time.monotonic() - started,
                limitations=limitations + ["WorkUnit has no application source file or failing test node."],
            )

        try:
            target_relative = self._relative_source(root, work_unit.source_file)
            source_snippet = self._source_snippet(root, work_unit, target_relative)
        except (OSError, UnicodeError, ValueError) as exc:
            return RepairReport(
                work_unit_id=work_unit.identifier,
                execution_mode=self.agent.execution_mode,
                final_status=RepairState.REJECTED,
                total_duration_seconds=time.monotonic() - started,
                limitations=limitations + [f"Source file is unavailable: {exc}"],
            )
        protected = snapshot_test_files(root)
        authorized = [target_relative]
        previous_feedback: str | None = None
        attempts: list[RepairAttempt] = []
        baseline_report = capture_baseline(
            root,
            timeout=self.regression_timeout,
            python_executable=self.python_executable,
        )

        for attempt_number in range(1, self.max_attempts + 1):
            raise_if_cancelled(cancellation_event)
            attempt = RepairAttempt(
                attempt_number=attempt_number,
                state_history=[
                    RepairState.PENDING,
                    RepairState.ANALYZING,
                ],
            )
            attempts.append(attempt)
            try:
                request = build_repair_request(
                    work_unit,
                    source_snippet=source_snippet,
                    authorized_paths=authorized,
                    previous_feedback=previous_feedback,
                ).model_copy(update={"target_file": target_relative})
                proposal = self.agent.generate_patch(request, previous_feedback)
                raise_if_cancelled(cancellation_event)
                if proposal.attempt_number != attempt_number:
                    raise InvalidAgentProposalError(
                        f"proposal attempt_number {proposal.attempt_number} does not match attempt {attempt_number}"
                    )
                attempt.proposal = proposal
                attempt.state_history.append(RepairState.PATCH_GENERATED)
            except AgentUnavailableError as exc:
                attempt.failure_reason = "agent_unavailable"
                attempt.feedback = str(exc)
                attempt.state_history.append(RepairState.FAILED)
                break
            except (InvalidAgentProposalError, ValueError, TypeError) as exc:
                attempt.failure_reason = "invalid_proposal"
                attempt.feedback = str(exc)
                attempt.state_history.extend([RepairState.REJECTED])
                previous_feedback = self._feedback(attempt)
                if attempt_number < self.max_attempts:
                    attempt.state_history.append(RepairState.RETRYING)
                    continue
                break

            attempt.state_history.append(RepairState.VALIDATING)
            with tempfile.TemporaryDirectory(prefix="faultline-repair-") as temporary:
                # Preserve the original directory name so package-style test
                # imports (for example ``from benchmarks...``) remain valid.
                copy_root = Path(temporary) / root.name
                shutil.copytree(root, copy_root, symlinks=True, ignore=_COPY_IGNORE)
                validation = validate_and_apply_patch(
                    proposal,
                    copy_root,
                    authorized_paths=authorized,
                    protected_paths=protected.files,
                )
                attempt.validation_result = validation
                if not validation.valid:
                    attempt.failure_reason = "syntax_error" if validation.syntax_error else "patch_rejected"
                    attempt.feedback = validation.reason
                    attempt.state_history.append(RepairState.REJECTED)
                else:
                    attempt.state_history.append(RepairState.TESTING)
                    raise_if_cancelled(cancellation_event)
                    test_result = run_targeted_test(
                        copy_root,
                        self._target_node_id(root, copy_root, work_unit.failing_test_node_id),
                        timeout=self.timeout,
                        python_executable=self.python_executable,
                    )
                    raise_if_cancelled(cancellation_event)
                    attempt.test_result = test_result
                    integrity = verify_test_integrity(copy_root, protected)
                    attempt.integrity_result = integrity
                    if not integrity.passed:
                        attempt.failure_reason = "test_integrity_violation"
                        attempt.feedback = integrity.reason
                        attempt.state_history.append(RepairState.REJECTED)
                    elif test_result.passed:
                        attempt.state_history.append(RepairState.PROVISIONALLY_SUCCESSFUL)
                        raise_if_cancelled(cancellation_event)
                        verification = verify_candidate(
                            baseline_report,
                            copy_root,
                            timeout=self.regression_timeout,
                            python_executable=self.python_executable,
                        )
                        raise_if_cancelled(cancellation_event)
                        decision = decide_acceptance(
                            patch_validation=validation,
                            targeted_test=test_result,
                            integrity=integrity,
                            verification=verification,
                        )
                        attempt.verification_report = verification
                        attempt.acceptance_decision = decision
                        if decision.status is RepairState.ACCEPTED:
                            mutation_report = None
                            if self.mutation_test:
                                mutation_report = self._run_mutation_quality(
                                    root,
                                    copy_root,
                                    work_unit,
                                    target_relative,
                                    verification,
                                )
                                attempt.mutation_report = mutation_report
                                verification.mutation_report = mutation_report
                            attempt.failure_reason = None
                            attempt.state_history.append(RepairState.ACCEPTED)
                            return RepairReport(
                                work_unit_id=work_unit.identifier,
                                execution_mode=self.agent.execution_mode,
                                attempts=attempts,
                                final_status=RepairState.ACCEPTED,
                                accepted_diff=proposal.unified_diff,
                                root_cause_explanation=proposal.root_cause_explanation,
                                test_integrity_passed=True,
                                targeted_test_passed=True,
                                verification_report=verification,
                                acceptance_decision=decision,
                                mutation_report=mutation_report,
                                acceptance_reason=decision.reason,
                                total_duration_seconds=time.monotonic() - started,
                                limitations=limitations,
                            )
                        if decision.status is RepairState.REJECTED:
                            attempt.failure_reason = "global_regression"
                            attempt.state_history.append(RepairState.REJECTED)
                        elif decision.status is RepairState.INCONCLUSIVE:
                            attempt.failure_reason = "verification_inconclusive"
                            attempt.state_history.append(RepairState.INCONCLUSIVE)
                        else:
                            attempt.failure_reason = "verification_inconclusive"
                            attempt.state_history.append(RepairState.FAILED)
                        attempt.feedback = self._feedback(attempt)
                    else:
                        attempt.failure_reason = self._test_failure_reason(test_result)
                        attempt.feedback = self._feedback(attempt)
                        attempt.state_history.append(RepairState.FAILED)

            previous_feedback = self._feedback(attempt)
            if attempt_number < self.max_attempts and attempt.failure_reason != "agent_unavailable":
                attempt.state_history.append(RepairState.RETRYING)
                continue
            break

        last_attempt = attempts[-1] if attempts else None
        if last_attempt and last_attempt.acceptance_decision:
            final = last_attempt.acceptance_decision.status
        elif last_attempt and last_attempt.failure_reason == "agent_unavailable":
            final = RepairState.FAILED
        elif last_attempt and last_attempt.failure_reason in {"patch_rejected", "syntax_error", "invalid_proposal"}:
            final = RepairState.REJECTED
        elif len(attempts) >= self.max_attempts:
            final = RepairState.FAILED
            if last_attempt and last_attempt.failure_reason not in {
                "test_integrity_violation",
                "patch_rejected",
                "syntax_error",
            }:
                last_attempt.failure_reason = "retry_exhausted"
        else:
            final = RepairState.REJECTED
        return RepairReport(
            work_unit_id=work_unit.identifier,
            execution_mode=self.agent.execution_mode,
            attempts=attempts,
            final_status=final,
            total_duration_seconds=time.monotonic() - started,
            test_integrity_passed=bool(last_attempt and last_attempt.integrity_result and last_attempt.integrity_result.passed),
            targeted_test_passed=bool(last_attempt and last_attempt.test_result and last_attempt.test_result.passed),
            verification_report=last_attempt.verification_report if last_attempt else None,
            acceptance_decision=last_attempt.acceptance_decision if last_attempt else None,
            acceptance_reason=(last_attempt.acceptance_decision.reason if last_attempt and last_attempt.acceptance_decision else None),
            limitations=limitations,
        )

    def _run_mutation_quality(
        self,
        original_root: Path,
        candidate_root: Path,
        work_unit: WorkUnit,
        target_relative: str,
        verification,
    ) -> MutationReport:
        """Run advisory mutation analysis only after mandatory acceptance passes."""
        target_path = candidate_root / Path(*target_relative.split("/"))
        try:
            source = target_path.read_text(encoding="utf-8")
            test_node_ids = self._mutation_test_nodes(
                original_root,
                candidate_root,
                work_unit,
                verification.candidate,
            )
            return run_mutation_testing(
                candidate_root,
                source=source,
                work_unit=work_unit,
                target_file=target_relative,
                baseline=verification.candidate,
                execution_mode=self.agent.execution_mode,
                test_node_ids=test_node_ids,
                max_mutants=self.max_mutants,
                timeout=self.mutation_timeout,
                python_executable=self.python_executable,
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            return MutationReport(
                work_unit_id=work_unit.identifier,
                execution_mode=self.agent.execution_mode,
                quality_warning=f"mutation analysis was inconclusive: {type(exc).__name__}: {exc}",
                limitations=[
                    "Mutation execution uses disposable temporary copies, not an OS-level security sandbox.",
                    "Mutation testing is advisory and does not alter mandatory patch acceptance.",
                ],
            )

    @classmethod
    def _mutation_test_nodes(
        cls,
        original_root: Path,
        candidate_root: Path,
        work_unit: WorkUnit,
        candidate_report,
    ) -> list[str]:
        """Choose only tests that passed on the unmutated verified candidate."""
        candidate_outcomes = candidate_report.outcomes
        requested = list(work_unit.test_node_ids)
        if work_unit.failing_test_node_id and work_unit.failing_test_node_id not in requested:
            requested.append(work_unit.failing_test_node_id)
        selected: list[str] = []
        for node_id in requested:
            normalized = cls._target_node_id(original_root, candidate_root, node_id)
            if candidate_outcomes.get(normalized) == "passed" and normalized not in selected:
                selected.append(normalized)
        return selected

    @staticmethod
    def _relative_source(root: Path, source_file: str) -> str:
        source = Path(source_file)
        if not source.is_absolute():
            source = root / source
        try:
            return source.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("WorkUnit source file is outside the project") from exc

    @staticmethod
    def _source_snippet(root: Path, work_unit: WorkUnit, relative: str) -> str:
        path = root / Path(*relative.split("/"))
        source = path.read_text(encoding="utf-8")
        line = work_unit.failures[0].source_location.line if work_unit.failures and work_unit.failures[0].source_location else 1
        lines = source.splitlines()
        start = max(0, line - 8)
        end = min(len(lines), line + 7)
        return "\n".join(lines[start:end])

    @staticmethod
    def _target_node_id(original_root: Path, copy_root: Path, node_id: str) -> str:
        """Normalize package-prefixed node ids when the project is a subdir."""
        prefix = original_root.name.replace("\\", "/") + "/"
        if node_id.startswith(prefix):
            candidate = node_id[len(prefix):]
            file_part = candidate.split("::", 1)[0]
            if (copy_root / Path(*file_part.split("/"))).exists():
                return candidate
        return node_id

    @staticmethod
    def _test_failure_reason(result: TestExecutionResult) -> FailureReason:
        if result.timed_out:
            return "test_timeout"
        if result.collection_error:
            return "test_collection_failure"
        return "test_assertion_failure"

    @staticmethod
    def _feedback(attempt: RepairAttempt) -> str:
        parts: list[str] = []
        if attempt.failure_reason:
            parts.append(f"failure_reason={attempt.failure_reason}")
        if attempt.proposal:
            parts.append("previous_candidate_diff:\n" + attempt.proposal.unified_diff)
        if attempt.validation_result and attempt.validation_result.reason:
            parts.append("patch_validation:\n" + attempt.validation_result.reason)
        if attempt.test_result:
            parts.append("test_stdout:\n" + attempt.test_result.stdout)
            parts.append("test_stderr:\n" + attempt.test_result.stderr)
        if attempt.integrity_result and attempt.integrity_result.reason:
            parts.append("integrity:\n" + attempt.integrity_result.reason)
        if attempt.acceptance_decision:
            parts.append("acceptance:\n" + attempt.acceptance_decision.reason)
        if attempt.verification_report and attempt.verification_report.failure_reason:
            parts.append("global_regression:\n" + attempt.verification_report.failure_reason)
        return "\n\n".join(parts) or "No diagnostic feedback was captured."


process_work_unit = RepairWorker.repair
