"""ghostqa run — Execute GhostQA behavioral tests.

This is the primary command. It resolves config, loads the orchestrator,
runs persona-driven journeys against a product, and displays live Rich
output with step progress, findings, and cost tracking.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostqa.config import GhostQAConfig, GhostQAConfigError
from ghostqa.credentials import mask_key, resolve_api_key

console = Console(stderr=True)
output_console = Console()  # stdout for machine-readable output

logger = logging.getLogger("ghostqa.cli.run")


def _parse_viewport(viewport_str: str) -> tuple[int, int]:
    """Parse a 'WIDTHxHEIGHT' string into a (width, height) tuple."""
    try:
        parts = viewport_str.lower().split("x")
        if len(parts) != 2:
            raise ValueError
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        console.print(
            Panel(
                f"[red]Invalid viewport format:[/red] {viewport_str}\n\n"
                "Expected format: WIDTHxHEIGHT (e.g., 1280x720)",
                title="[red]Config Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)


def _build_config(
    product: str,
    level: str,
    viewport: tuple[int, int],
    budget: float,
    headless: bool,
    project_dir: Path,
) -> GhostQAConfig:
    """Build a GhostQAConfig from CLI options, merging with config.yaml if present."""
    config_path = project_dir / "config.yaml"

    if config_path.is_file():
        config = GhostQAConfig.from_file(config_path)
    else:
        config = GhostQAConfig()
        config.project_dir = project_dir
        config.products_dir = project_dir / "products"
        config.personas_dir = project_dir / "personas"
        config.journeys_dir = project_dir / "journeys"
        config.evidence_dir = project_dir / "evidence"

    # CLI options override config file values
    config.product_name = product
    config.level = level
    config.viewport = viewport
    config.budget = budget
    config.headless = headless

    return config


def _resolve_project_dir() -> Path:
    """Find the .ghostqa/ project directory, searching upward from cwd."""
    current = Path.cwd()
    # Check current directory first
    candidate = current / ".ghostqa"
    if candidate.is_dir():
        return candidate

    # Walk up parents
    for parent in current.parents:
        candidate = parent / ".ghostqa"
        if candidate.is_dir():
            return candidate

    # Fallback: use cwd/.ghostqa (will be created if needed)
    return current / ".ghostqa"


def _print_run_header(
    product: str,
    journey: Optional[str],
    level: str,
    viewport: tuple[int, int],
    budget: float,
    headless: bool,
    api_key_display: str,
) -> None:
    """Print a styled header before the run starts."""
    info_lines = [
        f"[bold]Product:[/bold]   {product}",
        f"[bold]Journey:[/bold]   {journey or 'all'}",
        f"[bold]Level:[/bold]     {level}",
        f"[bold]Viewport:[/bold]  {viewport[0]}x{viewport[1]}",
        f"[bold]Budget:[/bold]    ${budget:.2f}",
        f"[bold]Headless:[/bold]  {headless}",
        f"[bold]API Key:[/bold]   {api_key_display}",
    ]
    console.print()
    console.print(
        Panel(
            "\n".join(info_lines),
            title="[bold cyan]GhostQA Run[/bold cyan]",
            border_style="cyan",
        )
    )
    console.print()


def _print_step_result(
    step_num: int,
    total_steps: int,
    step_id: str,
    description: str,
    passed: bool,
    duration: float,
    cost: float,
    error: Optional[str] = None,
    findings_count: int = 0,
) -> None:
    """Print a single step result line."""
    if passed:
        icon = "[bold green]\u2713[/bold green]"
        status = "[green]PASS[/green]"
    else:
        icon = "[bold red]\u2717[/bold red]"
        status = "[red]FAIL[/red]"

    # Main line: icon, step counter, description, status
    step_label = f"Step {step_num}/{total_steps}"
    line = Text()
    console.print(
        f"  {icon} {step_label}: {description}  {status}"
        f"  [dim]{duration:.1f}s  ${cost:.4f}[/dim]"
    )

    if error and not passed:
        # Show the error indented under the step
        error_short = error if len(error) <= 120 else error[:117] + "..."
        console.print(f"    [dim red]{error_short}[/dim red]")

    if findings_count > 0 and not passed:
        console.print(f"    [dim yellow]{findings_count} finding(s)[/dim yellow]")


def _print_summary_panel(
    all_passed: bool,
    total_steps: int,
    passed_steps: int,
    total_findings: int,
    duration: float,
    cost: float,
    run_id: str,
) -> None:
    """Print the final summary panel."""
    if all_passed:
        border = "green"
        verdict = "[bold green]ALL TESTS PASSED[/bold green]"
    else:
        border = "red"
        verdict = "[bold red]TESTS FAILED[/bold red]"

    summary_lines = [
        verdict,
        "",
        f"  Steps:     {passed_steps}/{total_steps} passed",
        f"  Findings:  {total_findings}",
        f"  Duration:  {duration:.1f}s",
        f"  Cost:      ${cost:.4f}",
        f"  Run ID:    {run_id}",
    ]

    console.print()
    console.print(Panel("\n".join(summary_lines), border_style=border))
    console.print()


def _write_junit_xml(
    junit_path: Path,
    run_id: str,
    product: str,
    step_reports: list,
    duration: float,
) -> None:
    """Write a JUnit XML report for CI integration."""
    import xml.etree.ElementTree as ET

    testsuite = ET.Element("testsuite")
    testsuite.set("name", f"ghostqa-{product}")
    testsuite.set("tests", str(len(step_reports)))
    testsuite.set("time", f"{duration:.2f}")

    failures = 0
    for step in step_reports:
        testcase = ET.SubElement(testsuite, "testcase")
        testcase.set("name", step.step_id)
        testcase.set("classname", f"ghostqa.{product}")
        testcase.set("time", f"{step.duration_seconds:.2f}")

        if not step.passed:
            failures += 1
            failure = ET.SubElement(testcase, "failure")
            failure.set("message", step.error or "Step failed")
            failure.text = step.error or ""

    testsuite.set("failures", str(failures))

    tree = ET.ElementTree(testsuite)
    ET.indent(tree, space="  ")
    tree.write(str(junit_path), xml_declaration=True, encoding="unicode")


def run(
    product: str = typer.Option(
        ...,
        "--product",
        "-p",
        help="Product name (must match a .yaml in .ghostqa/products/).",
    ),
    journey: Optional[str] = typer.Option(
        None,
        "--journey",
        "-j",
        help="Specific journey/scenario ID to run. Default: all journeys for the product.",
    ),
    level: str = typer.Option(
        "standard",
        "--level",
        "-l",
        help="Test level: smoke, standard, or thorough.",
    ),
    viewport: str = typer.Option(
        "1280x720",
        "--viewport",
        help="Browser viewport as WIDTHxHEIGHT.",
    ),
    budget: float = typer.Option(
        5.00,
        "--budget",
        "-b",
        help="Maximum budget for this run in USD.",
    ),
    junit_xml: Optional[Path] = typer.Option(
        None,
        "--junit-xml",
        help="Path to write JUnit XML report (for CI integration).",
    ),
    output_format: str = typer.Option(
        "text",
        "--output",
        "-o",
        help="Output format: text or json.",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--no-headless",
        help="Run browser in headless mode (default) or visible.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose logging.",
    ),
) -> None:
    """Run GhostQA behavioral tests against a product.

    Launches AI personas that navigate your app via real browser sessions,
    evaluating UX, functionality, and error handling through vision-based
    interaction.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s  %(message)s")

    # Validate level
    valid_levels = {"smoke", "standard", "thorough"}
    if level not in valid_levels:
        console.print(
            Panel(
                f"[red]Invalid level:[/red] {level}\n\n"
                f"Valid levels: {', '.join(sorted(valid_levels))}",
                title="[red]Config Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    # Validate output format
    if output_format not in ("text", "json"):
        console.print(
            Panel(
                f"[red]Invalid output format:[/red] {output_format}\n\n"
                "Valid formats: text, json",
                title="[red]Config Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    # Parse viewport
    vp = _parse_viewport(viewport)

    # Resolve project directory
    project_dir = _resolve_project_dir()

    # Resolve API key
    try:
        api_key = resolve_api_key(project_dir)
    except GhostQAConfigError as exc:
        console.print(
            Panel(
                f"[red]{exc}[/red]",
                title="[red]API Key Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    # Build config
    try:
        config = _build_config(
            product=product,
            level=level,
            viewport=vp,
            budget=budget,
            headless=headless,
            project_dir=project_dir,
        )
        config.anthropic_api_key = api_key
    except GhostQAConfigError as exc:
        console.print(
            Panel(
                f"[red]{exc}[/red]",
                title="[red]Config Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    # Print header (text mode only)
    if output_format == "text":
        _print_run_header(
            product=product,
            journey=journey,
            level=level,
            viewport=vp,
            budget=budget,
            headless=headless,
            api_key_display=mask_key(api_key),
        )

    # Import orchestrator (may fail if playwright not installed)
    try:
        from ghostqa.engine.orchestrator import GhostQAOrchestrator
    except ImportError as exc:
        console.print(
            Panel(
                f"[red]Failed to import GhostQA engine:[/red] {exc}\n\n"
                "This usually means a dependency is missing.\n"
                "Try: [bold]pip install ghostqa[/bold]\n"
                "Then: [bold]ghostqa install[/bold]",
                title="[red]Import Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=3)

    # Create orchestrator and run
    orchestrator = GhostQAOrchestrator(config)

    start_time = time.monotonic()

    if output_format == "text":
        console.print("[bold]Running tests...[/bold]\n")

    try:
        report_md, all_passed = orchestrator.run(
            product=product,
            scenario_id=journey,
            level=level,
            viewport=viewport if viewport != "1280x720" else None,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Run interrupted by user.[/yellow]")
        raise typer.Exit(code=1)
    except GhostQAConfigError as exc:
        console.print(
            Panel(
                f"[red]{exc}[/red]",
                title="[red]Config Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)
    except Exception as exc:
        logger.exception("Unexpected error during run")
        console.print(
            Panel(
                f"[red]Unexpected error:[/red] {exc}\n\n"
                "Run with [bold]--verbose[/bold] for full traceback.",
                title="[red]Infrastructure Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=3)

    duration = time.monotonic() - start_time

    # Try to extract structured info from the evidence directory for richer output
    # The orchestrator writes run-result.json to the evidence dir
    run_result_data = _load_latest_run_result(config.evidence_dir)

    if output_format == "json":
        # JSON output mode — dump the run result or a minimal structure
        if run_result_data:
            output_console.print(json.dumps(run_result_data, indent=2))
        else:
            output_console.print(json.dumps({
                "passed": all_passed,
                "report": report_md,
                "duration_seconds": round(duration, 2),
            }, indent=2))
    else:
        # Text output mode — rich formatting
        if run_result_data:
            _print_structured_results(run_result_data, duration)
        else:
            # Fallback: print the markdown report
            console.print()
            console.print(report_md)
            console.print()

    # Write JUnit XML if requested
    if junit_xml and run_result_data:
        try:
            from ghostqa.engine.report_generator import StepReport

            step_reports = []
            for sr in run_result_data.get("step_reports", []):
                step_reports.append(StepReport(
                    step_id=sr.get("step_id", "unknown"),
                    description=sr.get("description", ""),
                    mode=sr.get("mode", ""),
                    passed=sr.get("passed", False),
                    duration_seconds=sr.get("duration_seconds", 0),
                    error=sr.get("error"),
                ))
            _write_junit_xml(junit_xml, run_result_data.get("run_id", "unknown"), product, step_reports, duration)
            if output_format == "text":
                console.print(f"[dim]JUnit XML written to: {junit_xml}[/dim]\n")
        except Exception as exc:
            console.print(f"[yellow]Warning: Failed to write JUnit XML: {exc}[/yellow]")

    # Exit code: 0 = all pass, 1 = any fail
    if not all_passed:
        raise typer.Exit(code=1)


def _load_latest_run_result(evidence_dir: Path) -> dict | None:
    """Load the most recent run-result.json from the evidence directory."""
    if not evidence_dir.is_dir():
        return None

    # Find all run directories, sorted by name (they contain timestamps)
    run_dirs = sorted(
        [d for d in evidence_dir.iterdir() if d.is_dir() and d.name.startswith("GQA-RUN-")],
        reverse=True,
    )

    for run_dir in run_dirs:
        result_file = run_dir / "run-result.json"
        if result_file.is_file():
            try:
                return json.loads(result_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

    return None


def _print_structured_results(data: dict, duration: float) -> None:
    """Print structured results from a run-result.json dict."""
    step_reports = data.get("step_reports", [])
    findings = data.get("findings", [])
    total_steps = len(step_reports)
    passed_steps = sum(1 for s in step_reports if s.get("passed"))
    run_id = data.get("run_id", "unknown")
    cost = data.get("cost_usd", 0.0)
    all_passed = data.get("passed", False)

    # Print each step result
    for i, step in enumerate(step_reports, 1):
        step_findings = [f for f in findings if f.get("step_id") == step.get("step_id")]
        _print_step_result(
            step_num=i,
            total_steps=total_steps,
            step_id=step.get("step_id", "unknown"),
            description=step.get("description", ""),
            passed=step.get("passed", False),
            duration=step.get("duration_seconds", 0),
            cost=0,  # Per-step cost not tracked separately; shown in summary
            error=step.get("error"),
            findings_count=len(step_findings),
        )

    # Print findings table if any
    if findings:
        console.print()
        table = Table(title="Findings", border_style="red")
        table.add_column("Severity", style="bold")
        table.add_column("Category")
        table.add_column("Description")
        table.add_column("Step")

        for f in findings:
            severity = f.get("severity", "?")
            sev_style = {
                "block": "bold red",
                "critical": "red",
                "high": "yellow",
                "medium": "cyan",
                "low": "dim",
            }.get(severity, "")
            desc = f.get("description", "")
            if len(desc) > 80:
                desc = desc[:77] + "..."
            table.add_row(
                Text(severity, style=sev_style),
                f.get("category", "?"),
                desc,
                f.get("step_id", "?"),
            )
        console.print(table)

    # Print summary panel
    _print_summary_panel(
        all_passed=all_passed,
        total_steps=total_steps,
        passed_steps=passed_steps,
        total_findings=len(findings),
        duration=duration,
        cost=cost,
        run_id=run_id,
    )
