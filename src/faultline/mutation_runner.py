"""Execute and conservatively classify one generated mutant.

Mutation execution is intentionally independent from the mutation models.  A
caller supplies the unmutated candidate's node outcomes and receives a small,
JSON-friendly dataclass that can be copied into ``MutationResult`` later.

Each invocation copies the candidate into a temporary directory, replaces one
existing source file, and runs only the requested pytest node ids there.  The
candidate directory is never written to.  A mutant is considered killed only
when a node that passed in the baseline is reported as failed or errored in
the isolated run; all missing, malformed, skipped, timed-out, and
infrastructure results fail closed as inconclusive.
"""

from __future__ import annotations

import ast
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .integrity import snapshot_test_files, verify_test_integrity
from .regression import parse_regression_report_file
from .runner import _execution_environment, _terminate_process


KILLED = "KILLED"
SURVIVED = "SURVIVED"
INCONCLUSIVE = "INCONCLUSIVE"

_KNOWN_OUTCOMES = frozenset(
    {"passed", "failed", "skipped", "xfailed", "xpassed", "error", "unavailable"}
)
_EXPECTED_PYTEST_EXITS = frozenset({0, 1})
_MAX_EVIDENCE_OUTPUT = 64 * 1024
_BLOCKED_ENVIRONMENT_TOKENS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "API_KEY",
    "PRIVATE_KEY",
)


@dataclass
class MutationExecutionEvidence:
    """Plain evidence returned for one isolated mutant execution.

    ``outcome``, ``test_node_ids``, ``exit_code``, ``stdout``, ``stderr``,
    ``duration_seconds``, and ``failure_reason`` intentionally mirror the
    fields needed by the higher-level mutation result model.  The per-node
    mappings make the conservative decision auditable without requiring the
    runner to import that model.
    """

    outcome: str
    test_node_ids: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    failure_reason: str | None = None
    baseline_outcomes: dict[str, str] = field(default_factory=dict)
    candidate_outcomes: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Compatibility alias for callers that call the verdict a status."""
        return self.outcome

    @property
    def duration(self) -> float:
        """Compatibility alias for the measured execution duration."""
        return self.duration_seconds

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly evidence without exposing a temporary path."""
        return asdict(self)

    def model_dump(self) -> dict[str, Any]:
        """Small Pydantic-like convenience for integration adapters."""
        return self.as_dict()


@dataclass(frozen=True)
class _BaselineEvidence:
    outcomes: dict[str, str]
    timed_out: bool = False
    incomplete: bool = False
    report_error: str | None = None
    collection_errors: tuple[str, ...] = ()
    exit_code: int | None = None


@dataclass(frozen=True)
class _PytestRun:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    report: Any | None
    start_error: str | None = None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _redact_and_bound(value: Any) -> str:
    """Bound subprocess evidence and redact known secret environment values."""
    text = _text(value)
    for key, secret in os.environ.items():
        if (
            any(token in key.upper() for token in _BLOCKED_ENVIRONMENT_TOKENS)
            and secret
            and len(secret) >= 4
        ):
            text = text.replace(secret, "<redacted>")
    if len(text) <= _MAX_EVIDENCE_OUTPUT:
        return text
    return text[:_MAX_EVIDENCE_OUTPUT] + "\n...[output truncated]"


def _safe_reason(prefix: str, detail: Any = "") -> str:
    """Build a short reason without embedding command output or source text."""
    detail_text = _text(detail).strip().replace("\n", " ")
    if not detail_text:
        return prefix
    # Error messages can contain a local path or a value printed by a custom
    # subprocess wrapper.  Keep only a bounded, redacted diagnostic tail.
    return f"{prefix}: {_redact_and_bound(detail_text)[-512:]}"


def _unique_node_ids(values: Sequence[Any] | None) -> list[str]:
    if values is None or isinstance(values, (str, bytes)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    try:
        iterator = iter(values)
    except TypeError:
        return []
    for value in iterator:
        nodeid = _text(value).strip()
        if nodeid and nodeid not in seen:
            seen.add(nodeid)
            result.append(nodeid)
    return result


def _canonical_nodeid(value: Any, project_root: Path | None = None) -> str:
    """Normalize node-id path spelling while preserving selectors."""
    nodeid = _text(value).replace("\\", "/").strip()
    if not nodeid:
        return ""
    node_path, separator, selector = nodeid.partition("::")
    if project_root is not None:
        root_text = project_root.as_posix().rstrip("/")
        if node_path == root_text:
            node_path = "."
        elif node_path.startswith(root_text + "/"):
            node_path = node_path[len(root_text) + 1 :]
        else:
            try:
                candidate_path = Path(node_path).expanduser()
                if candidate_path.is_absolute():
                    node_path = candidate_path.resolve().relative_to(project_root).as_posix()
            except (OSError, ValueError):
                pass
    if node_path.startswith("./"):
        node_path = node_path[2:]
    return node_path + (separator + selector if separator else "")


def _normalise_outcome(value: Any) -> str | None:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bool):
        return "passed" if value else "failed"
    outcome = _text(value).strip().lower()
    if outcome in _KNOWN_OUTCOMES:
        return outcome
    return None


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _generated_mutant_value(mutant: Any, *names: str) -> Any:
    for name in names:
        value = _attribute(mutant, name)
        if value is not None:
            return value
    return None


def _report_outcomes(report: Any, project_root: Path | None) -> tuple[dict[str, str], bool]:
    """Extract stable node outcomes and report whether their shape was valid."""
    if report is None:
        return {}, False

    raw_outcomes = _attribute(report, "outcomes")
    outcomes: dict[str, str] = {}
    valid = True
    if isinstance(raw_outcomes, Mapping):
        for raw_nodeid, raw_outcome in raw_outcomes.items():
            nodeid = _canonical_nodeid(raw_nodeid, project_root)
            outcome = _normalise_outcome(raw_outcome)
            if not nodeid or outcome is None:
                valid = False
                continue
            outcomes[nodeid] = outcome

    raw_tests = _attribute(report, "tests")
    if raw_tests is not None:
        if not isinstance(raw_tests, Sequence) or isinstance(raw_tests, (str, bytes)):
            valid = False
        else:
            for raw_test in raw_tests:
                nodeid = _canonical_nodeid(_attribute(raw_test, "nodeid"), project_root)
                outcome = _normalise_outcome(_attribute(raw_test, "outcome"))
                if not nodeid or outcome is None:
                    valid = False
                    continue
                previous = outcomes.get(nodeid)
                if previous is not None and previous != outcome:
                    valid = False
                outcomes[nodeid] = outcome

    # A plain mapping of ``{nodeid: outcome}`` is the deliberately small input
    # form accepted by the public API.  It has no report-shaped keys to guide
    # the extraction above, so consume its entries directly.
    if not outcomes and isinstance(report, Mapping) and not any(
        key in report
        for key in (
            "outcomes",
            "tests",
            "passed_tests",
            "failed_tests",
            "skipped_tests",
            "unavailable_tests",
        )
    ):
        for raw_nodeid, raw_outcome in report.items():
            nodeid = _canonical_nodeid(raw_nodeid, project_root)
            outcome = _normalise_outcome(raw_outcome)
            if not nodeid or outcome is None:
                valid = False
                continue
            outcomes[nodeid] = outcome

    # Hand-built RegressionReport-compatible objects can expose only the
    # derived lists.  This fallback keeps the helper independent of Pydantic.
    if not outcomes:
        for attribute_name, outcome in (
            ("passed_tests", "passed"),
            ("failed_tests", "failed"),
            ("skipped_tests", "skipped"),
            ("unavailable_tests", "unavailable"),
        ):
            values = _attribute(report, attribute_name)
            if values is None:
                continue
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                valid = False
                continue
            for raw_nodeid in values:
                nodeid = _canonical_nodeid(raw_nodeid, project_root)
                if not nodeid:
                    valid = False
                    continue
                previous = outcomes.get(nodeid)
                if previous is not None and previous != outcome:
                    valid = False
                outcomes[nodeid] = outcome
    return outcomes, valid


def _baseline_evidence(baseline: Any, project_root: Path) -> _BaselineEvidence:
    """Accept a regression report, verification report, or plain mapping."""
    if baseline is None:
        return _BaselineEvidence({}, incomplete=True, report_error="baseline outcomes were not supplied")

    # A VerificationReport-like value contains the unmutated candidate under
    # ``candidate``.  Prefer it over any convenience properties on the wrapper.
    candidate = _attribute(baseline, "candidate")
    if candidate is not None:
        baseline = candidate
    elif isinstance(baseline, Mapping) and isinstance(baseline.get("candidate"), Mapping):
        baseline = baseline["candidate"]

    if isinstance(baseline, Mapping):
        if isinstance(baseline.get("outcomes"), Mapping):
            raw_outcomes: Any = baseline.get("outcomes")
        elif isinstance(baseline.get("tests"), Sequence) and not isinstance(
            baseline.get("tests"), (str, bytes)
        ):
            raw_outcomes = baseline
        else:
            # A plain {nodeid: outcome} mapping is the smallest supported API.
            raw_outcomes = baseline
    else:
        raw_outcomes = baseline

    outcomes, shape_valid = _report_outcomes(raw_outcomes, project_root)
    if isinstance(baseline, Mapping) and "summary" in baseline:
        summary = baseline.get("summary")
        raw_tests = baseline.get("tests")
        if not isinstance(summary, Mapping):
            shape_valid = False
        else:
            try:
                summary_total = int(summary.get("total"))
            except (TypeError, ValueError):
                shape_valid = False
            else:
                parsed_total = (
                    len(raw_tests)
                    if isinstance(raw_tests, Sequence) and not isinstance(raw_tests, (str, bytes))
                    else len(outcomes)
                )
                if summary_total != parsed_total:
                    shape_valid = False
    collection_errors_raw = _attribute(baseline, "collection_errors", ())
    if isinstance(collection_errors_raw, (str, bytes)):
        collection_errors = (_text(collection_errors_raw),)
    elif isinstance(collection_errors_raw, Sequence):
        collection_errors = tuple(_text(item) for item in collection_errors_raw if _text(item))
    else:
        collection_errors = ()
        shape_valid = False if collection_errors_raw is not None else shape_valid

    report_error = _attribute(baseline, "report_error")
    report_error_text = _text(report_error).strip() or None
    raw_exit_code = _attribute(baseline, "exit_code")
    try:
        exit_code = int(raw_exit_code) if raw_exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None
        shape_valid = False

    return _BaselineEvidence(
        outcomes=outcomes,
        timed_out=bool(_attribute(baseline, "timed_out", False)),
        incomplete=bool(_attribute(baseline, "incomplete", False)) or not shape_valid,
        report_error=report_error_text,
        collection_errors=collection_errors,
        exit_code=exit_code,
    )


def _target_relative_path(candidate_root: Path, target_file: str | Path) -> PurePosixPath:
    """Validate a target and return a safe candidate-relative POSIX path."""
    raw = _text(target_file).replace("\\", "/").strip()
    if not raw:
        raise ValueError("target file is missing")

    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or path.parts[0].endswith(":"):
        absolute = Path(raw).expanduser()
        if not absolute.is_absolute():
            raise ValueError("target file path is absolute or uses an unsupported drive")
        try:
            relative = absolute.resolve().relative_to(candidate_root)
        except (OSError, ValueError) as exc:
            raise ValueError("target file is outside the candidate") from exc
        path = PurePosixPath(relative.as_posix())

    if not path.parts or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        # ``.`` is harmless, but rejecting it keeps the recorded target path
        # deterministic and prevents odd path spellings around symlink checks.
        raise ValueError("target file path contains traversal or empty components")
    return path


def _contains_symlink(root: Path, destination: Path) -> bool:
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate_timeout(timeout: float) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


def _run_selected_tests(
    project_root: Path,
    node_ids: Sequence[str],
    *,
    timeout: float,
    python_executable: str | Path | None,
) -> _PytestRun:
    """Run selected nodes in one bounded process and parse its JSON report."""
    started = time.monotonic()
    executable = str(python_executable or sys.executable)
    with tempfile.TemporaryDirectory(prefix="faultline-mutant-report-") as temporary:
        report_path = Path(temporary) / "pytest-report.json"
        command = [
            executable,
            "-m",
            "pytest",
            "--json-report",
            f"--json-report-file={report_path}",
            "-q",
            *node_ids,
        ]
        try:
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                command,
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
                env=_execution_environment(project_root),
            )
        except (FileNotFoundError, OSError) as exc:
            return _PytestRun(
                exit_code=None,
                stdout="",
                stderr=_safe_reason("could not start pytest", exc.__class__.__name__),
                duration_seconds=time.monotonic() - started,
                timed_out=False,
                report=None,
                start_error="could not start pytest",
            )

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process)
            stdout, stderr = process.communicate()
            return _PytestRun(
                exit_code=124,
                stdout=_redact_and_bound(stdout or exc.stdout),
                stderr=_redact_and_bound(stderr or exc.stderr),
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                report=None,
            )

        exit_code = process.returncode
        try:
            report = parse_regression_report_file(
                report_path,
                project_root,
                exit_code=exit_code,
                duration_seconds=time.monotonic() - started,
            )
        except Exception:  # a malformed third-party report is infrastructure evidence
            return _PytestRun(
                exit_code=exit_code,
                stdout=_redact_and_bound(stdout),
                stderr=_redact_and_bound(stderr),
                duration_seconds=time.monotonic() - started,
                timed_out=False,
                report=None,
                start_error="could not parse pytest report",
            )
        return _PytestRun(
            exit_code=exit_code,
            stdout=_redact_and_bound(stdout),
            stderr=_redact_and_bound(stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=False,
            report=report,
        )


def _evidence(
    outcome: str,
    requested_ids: Sequence[str],
    *,
    started: float,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    failure_reason: str | None = None,
    baseline_outcomes: Mapping[str, str] | None = None,
    candidate_outcomes: Mapping[str, str] | None = None,
) -> MutationExecutionEvidence:
    return MutationExecutionEvidence(
        outcome=outcome,
        test_node_ids=list(requested_ids),
        exit_code=exit_code,
        stdout=_redact_and_bound(stdout),
        stderr=_redact_and_bound(stderr),
        duration_seconds=max(0.0, time.monotonic() - started),
        failure_reason=failure_reason,
        baseline_outcomes=dict(baseline_outcomes or {}),
        candidate_outcomes=dict(candidate_outcomes or {}),
    )


def evaluate_mutant(
    candidate_project_path: str | Path,
    target_file: str | Path | None = None,
    mutant_source: str | None = None,
    test_node_ids: Sequence[str] | None = None,
    baseline_outcomes: Any = None,
    *,
    mutant: Any = None,
    baseline: Any = None,
    timeout: float = 10.0,
    python_executable: str | Path | None = None,
) -> MutationExecutionEvidence:
    """Evaluate one mutant in an isolated disposable candidate copy.

    ``baseline_outcomes`` may be a plain ``{nodeid: outcome}`` mapping, a
    ``RegressionReport``-like object, or a ``VerificationReport``-like object
    (in which case its ``candidate`` report is used).  Only ordinary
    ``"passed"`` baseline nodes are eligible to kill a mutant.

    The timeout bounds the complete selected-node pytest invocation.  No shell
    is used, and the subprocess environment follows :mod:`faultline.runner`'s
    secret-filtering convention.

    For integration code that already has a ``GeneratedMutant``-like object,
    pass it as ``mutant=...``.  The object may expose ``target_file`` and any
    of ``source``, ``mutant_source``, or ``source_code``; explicit arguments
    take precedence.
    """
    started = time.monotonic()
    if mutant is not None:
        if target_file is None:
            target_file = _generated_mutant_value(mutant, "target_file")
        if mutant_source is None:
            mutant_source = _generated_mutant_value(
                mutant, "source", "mutant_source", "mutated_source", "source_code"
            )
        if test_node_ids is None:
            test_node_ids = _generated_mutant_value(mutant, "test_node_ids", "test_nodes")
    requested_ids = _unique_node_ids(test_node_ids)
    if baseline is not None:
        baseline_outcomes = baseline

    try:
        timeout_value = _validate_timeout(timeout)
    except ValueError as exc:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason=_safe_reason("invalid timeout", exc),
        )

    if not requested_ids:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="no pytest node ids were supplied",
        )

    if not isinstance(mutant_source, str) or not mutant_source:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="invalid mutant source: source must be a non-empty string",
        )
    try:
        ast.parse(mutant_source, filename=_text(target_file) or "<mutant>")
    except (SyntaxError, TypeError, ValueError, UnicodeError) as exc:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason=_safe_reason("invalid mutant source", exc.__class__.__name__),
        )

    try:
        candidate_root = Path(candidate_project_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason=_safe_reason("candidate path is invalid", exc.__class__.__name__),
        )
    if not candidate_root.is_dir():
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="candidate path is not a directory",
        )

    baseline_info = _baseline_evidence(baseline_outcomes, candidate_root)
    canonical_requested = list(
        dict.fromkeys(_canonical_nodeid(nodeid, candidate_root) for nodeid in requested_ids)
    )
    baseline_for_requested = {
        nodeid: baseline_info.outcomes[nodeid]
        for nodeid in canonical_requested
        if nodeid in baseline_info.outcomes
    }
    if baseline_info.timed_out:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="unmutated candidate baseline timed out",
            baseline_outcomes=baseline_for_requested,
        )
    if baseline_info.report_error:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="unmutated candidate baseline is malformed",
            baseline_outcomes=baseline_for_requested,
        )
    if baseline_info.collection_errors:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="unmutated candidate baseline has collection errors",
            baseline_outcomes=baseline_for_requested,
        )
    if baseline_info.incomplete:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="unmutated candidate baseline is incomplete",
            baseline_outcomes=baseline_for_requested,
        )
    if baseline_info.exit_code == 5:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="unmutated candidate baseline has no collected tests",
            baseline_outcomes=baseline_for_requested,
        )
    if baseline_info.exit_code is not None and baseline_info.exit_code not in {0, 1}:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason=f"unmutated candidate baseline exited unexpectedly with code {baseline_info.exit_code}",
            baseline_outcomes=baseline_for_requested,
        )
    missing_baseline = [
        nodeid for nodeid in canonical_requested if nodeid not in baseline_info.outcomes
    ]
    if missing_baseline:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="baseline is missing relevant node(s): " + ", ".join(missing_baseline),
            baseline_outcomes=baseline_for_requested,
        )
    not_passing_baseline = [
        f"{nodeid} ({baseline_info.outcomes[nodeid]})"
        for nodeid in canonical_requested
        if baseline_info.outcomes[nodeid] != "passed"
    ]
    if not_passing_baseline:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason="baseline relevant node(s) were not previously passing: "
            + ", ".join(not_passing_baseline),
            baseline_outcomes=baseline_for_requested,
        )

    try:
        relative_target = _target_relative_path(candidate_root, target_file)
    except (TypeError, ValueError) as exc:
        return _evidence(
            INCONCLUSIVE,
            requested_ids,
            started=started,
            failure_reason=_safe_reason("invalid target file", exc),
            baseline_outcomes=baseline_for_requested,
        )

    with tempfile.TemporaryDirectory(prefix="faultline-mutant-") as temporary:
        copy_root = Path(temporary) / candidate_root.name
        try:
            # Keep symlinks as links, matching repair.py's disposable-copy
            # convention, and reject a link at the mutation destination.
            shutil.copytree(candidate_root, copy_root, symlinks=True)
            destination = copy_root.joinpath(*relative_target.parts)
            if _contains_symlink(copy_root, destination):
                return _evidence(
                    INCONCLUSIVE,
                    requested_ids,
                    started=started,
                    failure_reason="target file is a symbolic link",
                    baseline_outcomes=baseline_for_requested,
                )
            if not destination.is_file():
                return _evidence(
                    INCONCLUSIVE,
                    requested_ids,
                    started=started,
                    failure_reason="target file is missing or not a regular file",
                    baseline_outcomes=baseline_for_requested,
                )
            protected_tests = snapshot_test_files(copy_root)
            destination.write_text(mutant_source, encoding="utf-8")
        except (OSError, UnicodeError, shutil.Error, ValueError) as exc:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                started=started,
                failure_reason=_safe_reason("could not prepare isolated mutant", exc.__class__.__name__),
                baseline_outcomes=baseline_for_requested,
            )

        run = _run_selected_tests(
            copy_root,
            canonical_requested,
            timeout=timeout_value,
            python_executable=python_executable,
        )
        common_kwargs = dict(
            started=started,
            exit_code=run.exit_code,
            stdout=run.stdout,
            stderr=run.stderr,
            baseline_outcomes=baseline_for_requested,
        )
        if run.timed_out:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason=f"pytest timed out after {timeout_value:g} seconds",
                **common_kwargs,
            )
        if run.start_error:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest infrastructure failure",
                **common_kwargs,
            )
        if run.report is None:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest report was not produced",
                **common_kwargs,
            )

        candidate_outcomes, candidate_shape_valid = _report_outcomes(run.report, copy_root)
        candidate_for_requested = {
            nodeid: candidate_outcomes[nodeid]
            for nodeid in canonical_requested
            if nodeid in candidate_outcomes
        }
        report_collection_errors = _attribute(run.report, "collection_errors", ())
        has_collection_errors = bool(report_collection_errors)
        report_error = _text(_attribute(run.report, "report_error")).strip()
        report_incomplete = bool(_attribute(run.report, "incomplete", False))
        if run.exit_code not in _EXPECTED_PYTEST_EXITS:
            reason = (
                "pytest collection failed"
                if run.exit_code == 2 or has_collection_errors
                else "pytest collected no relevant tests"
                if run.exit_code == 5
                else f"pytest exited unexpectedly with code {run.exit_code}"
            )
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason=reason,
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )
        if has_collection_errors:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest collection failed",
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )
        if report_error or report_incomplete or not candidate_shape_valid:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest report is incomplete or malformed",
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )

        integrity = verify_test_integrity(copy_root, protected_tests)
        if not integrity.passed:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason=integrity.reason or "mutant execution changed protected test files",
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )

        missing_candidate = [
            nodeid for nodeid in canonical_requested if nodeid not in candidate_outcomes
        ]
        if missing_candidate:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="mutant run is missing relevant node(s): " + ", ".join(missing_candidate),
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )

        non_passing = {
            nodeid: candidate_outcomes[nodeid]
            for nodeid in canonical_requested
            if candidate_outcomes[nodeid] != "passed"
        }
        if run.exit_code == 0 and any(
            outcome in {"failed", "error"} for outcome in non_passing.values()
        ):
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest report and exit code disagree",
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )
        if run.exit_code == 1 and not non_passing:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="pytest exited with code 1 without a failed relevant test",
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )

        non_failure_non_passing = {
            nodeid: outcome
            for nodeid, outcome in non_passing.items()
            if outcome not in {"failed", "error"}
        }
        if non_failure_non_passing:
            return _evidence(
                INCONCLUSIVE,
                requested_ids,
                failure_reason="relevant node(s) became non-passing without a failure: "
                + ", ".join(
                    f"{nodeid} ({candidate_outcomes[nodeid]})"
                    for nodeid in non_failure_non_passing
                ),
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )

        killed_nodes = [
            nodeid
            for nodeid in canonical_requested
            if candidate_outcomes[nodeid] in {"failed", "error"}
        ]
        if killed_nodes:
            return _evidence(
                KILLED,
                requested_ids,
                failure_reason="newly non-passing relevant node(s): " + ", ".join(killed_nodes),
                candidate_outcomes=candidate_for_requested,
                **common_kwargs,
            )
        return _evidence(
            SURVIVED,
            requested_ids,
            candidate_outcomes=candidate_for_requested,
            **common_kwargs,
        )


def evaluate_generated_mutant(
    candidate_project_path: str | Path,
    mutant: Any,
    test_node_ids: Sequence[str] | None = None,
    baseline_outcomes: Any = None,
    *,
    baseline: Any = None,
    timeout: float = 10.0,
    python_executable: str | Path | None = None,
) -> MutationExecutionEvidence:
    """Evaluate a generated-mutant object without importing its model."""
    return evaluate_mutant(
        candidate_project_path,
        test_node_ids=test_node_ids,
        baseline_outcomes=baseline_outcomes,
        mutant=mutant,
        baseline=baseline,
        timeout=timeout,
        python_executable=python_executable,
    )


# Descriptive aliases keep the one-mutant integration seam easy to discover.
run_mutant = evaluate_mutant
execute_mutant = evaluate_mutant
evaluate_mutation = evaluate_mutant


__all__ = [
    "INCONCLUSIVE",
    "KILLED",
    "SURVIVED",
    "MutationExecutionEvidence",
    "evaluate_mutant",
    "evaluate_generated_mutant",
    "evaluate_mutation",
    "execute_mutant",
    "run_mutant",
]
