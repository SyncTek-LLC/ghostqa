"""ghostqa report — View and export run reports.

Lists past runs, displays individual reports in markdown or JSON,
and optionally opens the HTML report in a browser.
"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


def _find_evidence_dir() -> Path:
    """Locate the .ghostqa/evidence/ directory by searching upward from cwd."""
    current = Path.cwd()
    for base in [current, *current.parents]:
        candidate = base / ".ghostqa" / "evidence"
        if candidate.is_dir():
            return candidate
    return current / ".ghostqa" / "evidence"


def _list_runs(evidence_dir: Path) -> list[dict]:
    """Scan evidence directory and return metadata for all runs."""
    runs: list[dict] = []

    if not evidence_dir.is_dir():
        return runs

    for run_dir in sorted(evidence_dir.iterdir(), reverse=True):
        if not run_dir.is_dir() or not run_dir.name.startswith("GQA-RUN-"):
            continue

        meta: dict = {"run_id": run_dir.name, "dir": str(run_dir)}

        # Try run-result.json for detailed info
        result_file = run_dir / "run-result.json"
        if result_file.is_file():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                meta["product"] = data.get("product_name", "?")
                meta["scenario"] = data.get("scenario_name", "?")
                meta["passed"] = data.get("passed", None)
                meta["cost"] = data.get("cost_usd", 0.0)
                meta["duration"] = data.get("duration_seconds", 0.0)
                meta["start_time"] = data.get("start_time", "?")
                steps = data.get("step_reports", [])
                passed_count = sum(1 for s in steps if s.get("passed"))
                meta["steps"] = f"{passed_count}/{len(steps)}"
            except (json.JSONDecodeError, OSError):
                pass

        # Fall back to run-status.json
        if "product" not in meta:
            status_file = run_dir / "run-status.json"
            if status_file.is_file():
                try:
                    data = json.loads(status_file.read_text(encoding="utf-8"))
                    meta["product"] = data.get("product", "?")
                    meta["scenario"] = data.get("scenario", "?")
                    meta["passed"] = data.get("passed", None)
                    meta["start_time"] = data.get("start_time", "?")
                except (json.JSONDecodeError, OSError):
                    pass

        runs.append(meta)

    return runs


def _find_run_dir(evidence_dir: Path, run_id: str | None) -> Path | None:
    """Find a run directory by ID, or return the latest run."""
    if not evidence_dir.is_dir():
        return None

    if run_id:
        # Exact match
        candidate = evidence_dir / run_id
        if candidate.is_dir():
            return candidate

        # Partial match (prefix)
        matches = [d for d in evidence_dir.iterdir() if d.is_dir() and d.name.startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            console.print(f"[yellow]Ambiguous run ID '{run_id}' matches {len(matches)} runs.[/yellow]")
            return None
        return None

    # Latest run
    run_dirs = sorted(
        [d for d in evidence_dir.iterdir() if d.is_dir() and d.name.startswith("GQA-RUN-")],
        reverse=True,
    )
    return run_dirs[0] if run_dirs else None


def report(
    run_id: str | None = typer.Argument(
        None,
        help="Run ID to display (default: latest run). Supports prefix matching.",
    ),
    list_runs: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List all recorded runs.",
    ),
    format: str = typer.Option(
        "markdown",
        "--format",
        "-f",
        help="Report format: markdown or json.",
    ),
    open_report: bool = typer.Option(
        False,
        "--open",
        help="Open the report in the default browser.",
    ),
    evidence_dir: Path | None = typer.Option(
        None,
        "--evidence-dir",
        help="Path to evidence directory. Default: .ghostqa/evidence/",
    ),
) -> None:
    """View or export GhostQA run reports.

    Without arguments, shows the latest run report. Use --list to see
    all recorded runs, or provide a RUN_ID to view a specific run.
    """
    edir = evidence_dir or _find_evidence_dir()

    if not edir.is_dir():
        console.print(
            Panel(
                f"[yellow]Evidence directory not found:[/yellow] {edir}\n\n"
                "No runs recorded yet. Run [bold]ghostqa run --product <name>[/bold] first.",
                title="[yellow]No Evidence[/yellow]",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=0)

    # --list mode: show table of all runs
    if list_runs:
        runs = _list_runs(edir)
        if not runs:
            console.print("[yellow]No runs found.[/yellow]")
            raise typer.Exit(code=0)

        table = Table(title="GhostQA Runs", border_style="cyan")
        table.add_column("Run ID", style="bold")
        table.add_column("Product")
        table.add_column("Scenario")
        table.add_column("Result")
        table.add_column("Steps")
        table.add_column("Cost")
        table.add_column("Duration")
        table.add_column("Time")

        for r in runs:
            passed = r.get("passed")
            if passed is True:
                result_str = "[green]PASS[/green]"
            elif passed is False:
                result_str = "[red]FAIL[/red]"
            else:
                result_str = "[dim]?[/dim]"

            cost = r.get("cost", 0)
            cost_str = f"${cost:.4f}" if cost else "-"
            dur = r.get("duration", 0)
            dur_str = f"{dur:.1f}s" if dur else "-"

            table.add_row(
                r.get("run_id", "?"),
                r.get("product", "?"),
                r.get("scenario", "?"),
                result_str,
                r.get("steps", "-"),
                cost_str,
                dur_str,
                r.get("start_time", "?"),
            )

        console.print()
        console.print(table)
        console.print()
        return

    # Single run view
    run_dir = _find_run_dir(edir, run_id)
    if run_dir is None:
        if run_id:
            console.print(f"[red]Run not found:[/red] {run_id}")
        else:
            console.print("[yellow]No runs found. Run a test first.[/yellow]")
        raise typer.Exit(code=1)

    if format == "json":
        result_file = run_dir / "run-result.json"
        if result_file.is_file():
            data = json.loads(result_file.read_text(encoding="utf-8"))
            console.print(json.dumps(data, indent=2))
        else:
            console.print(f"[yellow]No structured result for run {run_dir.name}[/yellow]")
            raise typer.Exit(code=1)
    else:
        # Markdown report
        report_file = run_dir / "report.md"
        if report_file.is_file():
            md_content = report_file.read_text(encoding="utf-8")
            console.print()
            console.print(Markdown(md_content))
            console.print()
        else:
            # Fall back to JSON pretty-print
            result_file = run_dir / "run-result.json"
            if result_file.is_file():
                data = json.loads(result_file.read_text(encoding="utf-8"))
                console.print(json.dumps(data, indent=2))
            else:
                console.print(f"[yellow]No report found for run {run_dir.name}[/yellow]")
                raise typer.Exit(code=1)

    # --open: open report in browser
    if open_report:
        report_file = run_dir / "report.md"
        if report_file.is_file():
            webbrowser.open(f"file://{report_file.resolve()}")
            console.print(f"[dim]Opened: {report_file}[/dim]")
        else:
            console.print("[yellow]No report file to open.[/yellow]")
