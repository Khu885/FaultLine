"""Typer and Rich command-line interface for FaultLine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .agent_adapter import (
    AgentUnavailableError,
    FixtureAgentAdapter,
    OmniRushHumanMediatedAdapter,
    UnavailableAgentAdapter,
    load_proposal,
)
from .collector import collect_project
from .models import RepairState, ScanReport, WorkUnit
from .worker import RepairWorker

app = typer.Typer(help="Deterministic pytest failure analysis.", no_args_is_help=True)
console = Console()


@app.callback()
def callback(version: bool = typer.Option(False, "--version", help="Show the FaultLine version.", is_eager=True)) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def scan(
    project_path: Annotated[Path, typer.Argument(help="Project directory containing pytest tests.")],
    json_output: Annotated[Path | None, typer.Option("--json-output", help="Write the structured scan report to this path.")] = None,
) -> None:
    """Collect pytest failures and group them into conservative work units."""
    report = collect_project(project_path)
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    if report.collection_error:
        console.print(f"[yellow]Collection error:[/yellow] {report.collection_error}")
    elif report.report_error:
        console.print(f"[yellow]Report warning:[/yellow] {report.report_error}")
    console.print(f"Collected {report.test_count} test(s); {len(report.failures)} failure(s) in {len(report.work_units)} work unit(s).")
    for failure in report.failures:
        location = failure.source_location
        suffix = f" ({location.path}:{location.line})" if location and location.path else ""
        console.print(f"[red]FAIL[/red] {failure.nodeid}{suffix}")


def _select_work_unit(report: ScanReport, selection: str | None) -> WorkUnit:
    if not report.work_units:
        raise typer.BadParameter("scan produced no repairable work units")
    if selection is None:
        return report.work_units[0]
    for unit in report.work_units:
        if unit.identifier == selection:
            return unit
    try:
        index = int(selection)
    except ValueError:
        index = -1
    if 0 <= index < len(report.work_units):
        return report.work_units[index]
    raise typer.BadParameter(f"unknown WorkUnit: {selection}")


def _write_json(path: Path | None, payload: object) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _adapter_for(
    backend: str,
    *,
    request_output: Path | None,
    proposal_input: Path | None,
):
    normalized = backend.strip().lower()
    if normalized == "fixture":
        if proposal_input is None:
            return FixtureAgentAdapter()
        return FixtureAgentAdapter([load_proposal(proposal_input)])
    if normalized in {"omnirush", "human", "human-mediated"}:
        return OmniRushHumanMediatedAdapter(
            request_output=request_output,
            proposal_input=proposal_input,
        )
    if normalized in {"unavailable", "none"}:
        return UnavailableAgentAdapter()
    raise typer.BadParameter("backend must be one of: omnirush, fixture, unavailable")


def _run_repair(
    project_path: Path,
    *,
    max_attempts: int,
    backend: str,
    work_unit: str | None,
    json_output: Path | None,
    timeout: float,
    verbose: bool,
    request_output: Path | None,
    proposal_input: Path | None,
) -> int:
    report = collect_project(project_path)
    if report.collection_error:
        console.print(f"[yellow]Collection error:[/yellow] {report.collection_error}")
        return 2
    try:
        selected = _select_work_unit(report, work_unit)
        adapter = _adapter_for(
            backend,
            request_output=request_output,
            proposal_input=proposal_input,
        )
        repair_report = RepairWorker(adapter, max_attempts=max_attempts, timeout=timeout).repair(
            project_path,
            selected,
        )
    except (AgentUnavailableError, ValueError, OSError) as exc:
        console.print(f"[red]Repair setup failed:[/red] {exc}")
        return 2

    _write_json(json_output, repair_report.model_dump(mode="json"))
    console.print(
        f"WorkUnit {selected.identifier}: [bold]{repair_report.final_status}[/bold] "
        f"({len(repair_report.attempts)} attempt(s), mode={repair_report.execution_mode})"
    )
    for attempt in repair_report.attempts:
        console.print(
            f"  attempt {attempt.attempt_number}: "
            f"{attempt.failure_reason or 'no failure'}; states="
            f"{' -> '.join(attempt.state_history)}"
        )
        if attempt.proposal:
            console.print("  candidate diff:")
            console.print(attempt.proposal.unified_diff)
        if verbose and attempt.feedback:
            console.print("  feedback:")
            console.print(attempt.feedback)
    if request_output and backend.lower() in {"omnirush", "human", "human-mediated"}:
        console.print(f"Repair request/proposal handoff: {request_output}")
    if json_output:
        console.print(f"Repair report: {json_output}")
    return 0 if repair_report.final_status is RepairState.PROVISIONALLY_SUCCESSFUL else 1


@app.command()
def repair(
    project_path: Annotated[Path, typer.Argument(help="Project directory containing pytest tests.")],
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 2,
    backend: Annotated[str, typer.Option("--backend", help="omnirush, fixture, or unavailable.")] = "omnirush",
    work_unit: Annotated[str | None, typer.Option("--work-unit", help="WorkUnit id or zero-based index.")] = None,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1)] = 5.0,
    verbose: Annotated[bool, typer.Option("--verbose/--no-verbose")] = False,
    request_output: Annotated[Path | None, typer.Option("--request-output", help="Export human-mediated OmniRush request here.")] = None,
    proposal_input: Annotated[Path | None, typer.Option("--proposal-input", help="Import a structured proposal JSON file.")] = None,
) -> None:
    """Repair one detected WorkUnit in a disposable copy and verify it."""
    raise typer.Exit(
        _run_repair(
            project_path,
            max_attempts=max_attempts,
            backend=backend,
            work_unit=work_unit,
            json_output=json_output,
            timeout=timeout,
            verbose=verbose,
            request_output=request_output,
            proposal_input=proposal_input,
        )
    )


@app.command("repair-export")
def repair_export(
    project_path: Annotated[Path, typer.Argument(help="Project directory containing pytest tests.")],
    output: Annotated[Path, typer.Option("--output", help="Request JSON destination.")],
    work_unit: Annotated[str | None, typer.Option("--work-unit")] = None,
) -> None:
    """Export a repair request for processing by an actual OmniRush session."""
    report = collect_project(project_path)
    if report.collection_error:
        console.print(f"[yellow]Collection error:[/yellow] {report.collection_error}")
        raise typer.Exit(2)
    selected = _select_work_unit(report, work_unit)
    adapter = OmniRushHumanMediatedAdapter(request_output=output)
    try:
        adapter.generate_patch(
            _request_for_export(project_path, selected),
        )
    except AgentUnavailableError:
        pass
    console.print(f"Exported OmniRush repair request for {selected.identifier} to {output}")


@app.command("repair-import")
def repair_import(
    project_path: Annotated[Path, typer.Argument(help="Project directory containing pytest tests.")],
    proposal: Annotated[Path, typer.Option("--proposal", help="Proposal JSON returned by an OmniRush session.")],
    work_unit: Annotated[str | None, typer.Option("--work-unit")] = None,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 2,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1)] = 5.0,
) -> None:
    """Import an OmniRush proposal and validate it through the repair worker."""
    raise typer.Exit(
        _run_repair(
            project_path,
            max_attempts=max_attempts,
            backend="omnirush",
            work_unit=work_unit,
            json_output=json_output,
            timeout=timeout,
            verbose=False,
            request_output=None,
            proposal_input=proposal,
        )
    )


def _request_for_export(project_path: Path, work_unit: WorkUnit):
    from .agent_adapter import build_repair_request

    root = project_path.expanduser().resolve()
    source = Path(work_unit.source_file or "")
    if not source.is_absolute():
        source = root / source
    relative = source.resolve().relative_to(root).as_posix()
    lines = source.read_text(encoding="utf-8").splitlines()
    line = work_unit.failures[0].source_location.line if work_unit.failures and work_unit.failures[0].source_location else 1
    return build_repair_request(
        work_unit,
        source_snippet="\n".join(lines[max(0, line - 8): min(len(lines), line + 7)]),
        authorized_paths=[relative],
    ).model_copy(update={"target_file": relative})


def _load_work_units(path: Path) -> list[WorkUnit]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "work_units" in payload:
        payload = payload["work_units"]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("input must be a WorkUnit list or an object with work_units")
    return [WorkUnit.model_validate(item) for item in payload]


def _batch_adapter(backend: str, proposal_input: Path | None):
    if proposal_input is not None and proposal_input.is_dir():
        proposals = [load_proposal(path) for path in sorted(proposal_input.glob("*.json"))]
        return FixtureAgentAdapter(proposals)
    return _adapter_for(backend, request_output=None, proposal_input=proposal_input)


@app.command("repair-batch")
def repair_batch(
    project_path: Annotated[Path, typer.Argument(help="Project directory containing pytest tests.")],
    input_path: Annotated[Path, typer.Option("--input", help="WorkUnit JSON or scan report JSON.")],
    max_workers: Annotated[int, typer.Option("--max-workers", min=1, max=32)] = 3,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 2,
    backend: Annotated[str, typer.Option("--backend")] = "omnirush",
    timeout: Annotated[float, typer.Option("--timeout", min=0.1)] = 5.0,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
    proposal_input: Annotated[Path | None, typer.Option("--proposal-input", help="Proposal JSON or directory of fixture proposals.")] = None,
) -> None:
    """Repair independent WorkUnits concurrently in isolated copies."""
    try:
        units = _load_work_units(input_path)
        worker = RepairWorker(_batch_adapter(backend, proposal_input), max_attempts=max_attempts, timeout=timeout)
        aggregate = RepairOrchestrator(worker, max_workers=max_workers, worker_timeout=timeout).execute(project_path, units)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Batch setup failed:[/red] {exc}")
        raise typer.Exit(2)
    _write_json(json_output, aggregate.model_dump(mode="json"))
    console.print(
        f"Processed {aggregate.total_work_units} WorkUnit(s): {aggregate.successful_repairs} succeeded, "
        f"{aggregate.failed_repairs} failed, {aggregate.rejected_patches} rejected, "
        f"{aggregate.timed_out_workers} timed out."
    )
    raise typer.Exit(0 if not aggregate.failed_repairs and not aggregate.rejected_patches and not aggregate.timed_out_workers else 1)


def main(argv: list[str] | None = None) -> int:
    """Entry point usable by both the console script and ordinary unit tests."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--version" in arguments:
        typer.echo(__version__)
        return 0
    try:
        result = app(args=arguments, standalone_mode=False)
        return int(result) if isinstance(result, int) else 0
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except (typer.BadParameter, OSError, ValueError) as exc:
        console.print(f"[red]faultline:[/red] {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
