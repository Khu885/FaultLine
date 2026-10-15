"""Typer and Rich command-line interface for FaultLine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .collector import collect_project

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


def main(argv: list[str] | None = None) -> int:
    """Entry point usable by both the console script and ordinary unit tests."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--version" in arguments:
        typer.echo(__version__)
        return 0
    try:
        app(args=arguments, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except (typer.BadParameter, OSError, ValueError) as exc:
        console.print(f"[red]faultline:[/red] {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
