"""specterqa run — Execute SpecterQA behavioral tests.

This is the primary command. It resolves config, loads the orchestrator,
runs persona-driven journeys against a product, and displays live Rich
output with step progress, findings, and cost tracking.

Features:
- TTY-aware output: Rich spinner and formatting only in interactive terminals;
  plain ASCII line-by-line output in CI/pipes (auto-detected or via --plain).
- --fail-on-severity: Exit 1 only when findings meet or exceed a threshold.
- --plain: Explicit ASCII mode for screen readers and minimal terminals.
- Findings sorted by severity in all output modes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from specterqa.config import SpecterQAConfig, SpecterQAConfigError
from specterqa.credentials import mask_key, resolve_api_key

console = Console(stderr=True)
output_console = Console()  # stdout for machine-readable output

logger = logging.getLogger("specterqa.cli.run")

# ── Environment variable helpers ──────────────────────────────────────────

_ENV_BUDGET_KEY = "SPECTERQA_BUDGET"


def _resolve_budget_default() -> float:
    """Resolve the default budget from the SPECTERQA_BUDGET env var, if set."""
    env_val = os.environ.get(_ENV_BUDGET_KEY)
    if env_val is not None:
        try:
            val = float(env_val)
            if val <= 0:
                raise ValueError
            return val
        except ValueError:
            logger.warning(
                "Ignoring invalid %s value: %r (expected a positive number)",
                _ENV_BUDGET_KEY,
                env_val,
            )
    return DEFAULT_BUDGET_USD

# ── Severity ordering ─────────────────────────────────────────────────────

# Ordered most-severe first; 'any' is a special sentinel for "fail on everything"
_SEVERITY_LEVELS = ["block", "critical", "high", "medium", "low", "any"]
_SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(_SEVERITY_LEVELS)}


def _finding_meets_threshold(finding_severity: str, threshold: str) -> bool:
    """Return True if a finding severity is at or above the given threshold.

    Severity hierarchy (most severe first): block > critical > high > medium > low.
    'any' means every finding qualifies.
    """
    if threshold == "any":
        return True
    finding_rank = _SEVERITY_ORDER.get(finding_severity, len(_SEVERITY_ORDER))
    threshold_rank = _SEVERITY_ORDER.get(threshold, len(_SEVERITY_ORDER))
    return finding_rank <= threshold_rank


def _sort_findings_by_severity(findings: list[dict]) -> list[dict]:
    """Sort findings from most to least severe."""
    return sorted(
        findings,
        key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "low"), len(_SEVERITY_ORDER)),
    )


# ── Plain-mode output helpers ─────────────────────────────────────────────


def _plain_print(msg: str) -> None:
    """Print a plain text line to stderr."""
    print(msg, file=sys.stderr, flush=True)


# ── Shared error printer ──────────────────────────────────────────────────


def _print_error(c: Console, plain: bool, message: str, title: str = "Error") -> None:
    """Print an error message in either plain or Rich mode."""
    if plain:
        _plain_print(f"[{title}] {message}")
    else:
        c.print(Panel(f"[red]{message}[/red]", title=f"[red]{title}[/red]", border_style="red"))


# ── Viewport parser ───────────────────────────────────────────────────────


def _parse_viewport(viewport_str: str, plain: bool) -> tuple[int, int]:
    """Parse a 'WIDTHxHEIGHT' string into a (width, height) tuple."""
    try:
        parts = viewport_str.lower().split("x")
        if len(parts) != 2:
            raise ValueError
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        _print_error(
            console,
            plain,
            f"Invalid viewport format: {viewport_str}\n\nExpected format: WIDTHxHEIGHT (e.g., 1280x720)",
            "Config Error",
        )
        raise typer.Exit(code=2)


# ── Config builder ────────────────────────────────────────────────────────


def _build_config(
    product: str,
    level: str,
    viewport: tuple[int, int],
    budget: float,
    headless: bool,
    project_dir: Path,
) -> SpecterQAConfig:
    """Build a SpecterQAConfig from CLI options, merging with config.yaml if present."""
    config_path = project_dir / "config.yaml"

    if config_path.is_file():
        config = SpecterQAConfig.from_file(config_path)
    else:
        config = SpecterQAConfig()
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
    """Find the .specterqa/ project directory, searching upward from cwd."""
    current = Path.cwd()
    candidate = current / ".specterqa"
    if candidate.is_dir():
        return candidate
    for parent in current.parents:
        candidate = parent / ".specterqa"
        if candidate.is_dir():
            return candidate
    return current / ".specterqa"


# ── Rich output helpers ───────────────────────────────────────────────────


def _print_run_header_rich(
    product: str,
    journey: str | None,
    level: str,
    viewport: tuple[int, int],
    budget: float,
    headless: bool,
    api_key_display: str,
    fail_on_severity: str,
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
        f"[bold]Fail on:[/bold]   {fail_on_severity}",
    ]
    console.print()
    console.print(
        Panel(
            "\n".join(info_lines),
            title="[bold cyan]SpecterQA Run[/bold cyan]",
            border_style="cyan",
        )
    )
    console.print()


def _print_step_result_rich(
    step_num: int,
    total_steps: int,
    description: str,
    passed: bool,
    duration: float,
    error: str | None = None,
    findings_count: int = 0,
) -> None:
    """Print a single step result line in Rich mode."""
    if passed:
        icon = "[bold green]\u2713[/bold green]"
        status = "[green]PASS[/green]"
    else:
        icon = "[bold red]\u2717[/bold red]"
        status = "[red]FAIL[/red]"

    step_label = f"Step {step_num}/{total_steps}"
    console.print(f"  {icon} {step_label}: {description}  {status}  [dim]{duration:.1f}s[/dim]")

    if error and not passed:
        error_short = error if len(error) <= 120 else error[:117] + "..."
        console.print(f"    [dim red]{error_short}[/dim red]")

    if findings_count > 0 and not passed:
        console.print(f"    [dim yellow]{findings_count} finding(s)[/dim yellow]")


def _print_summary_panel_rich(
    all_passed: bool,
    total_steps: int,
    passed_steps: int,
    total_findings: int,
    duration: float,
    cost: float,
    run_id: str,
) -> None:
    """Print the final summary panel in Rich mode."""
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


# ── Plain (ASCII) output helpers ──────────────────────────────────────────


def _print_run_header_plain(
    product: str,
    journey: str | None,
    level: str,
    viewport: tuple[int, int],
    budget: float,
    headless: bool,
    fail_on_severity: str,
) -> None:
    """Print a plain-text header for CI/screen-reader contexts."""
    _plain_print(
        f"SpecterQA run starting: product={product} journey={journey or 'all'} "
        f"level={level} viewport={viewport[0]}x{viewport[1]} "
        f"budget=${budget:.2f} headless={headless} fail-on={fail_on_severity}"
    )


def _print_step_result_plain(
    step_num: int,
    total_steps: int,
    description: str,
    passed: bool,
    duration: float,
    error: str | None = None,
    findings_count: int = 0,
) -> None:
    """Print a single step result line in plain ASCII mode."""
    status = "PASS" if passed else "FAIL"
    _plain_print(f"Step {step_num}/{total_steps} {status}: {description} ({duration:.1f}s)")
    if error and not passed:
        error_short = error if len(error) <= 160 else error[:157] + "..."
        _plain_print(f"  Error: {error_short}")
    if findings_count > 0:
        _plain_print(f"  Findings: {findings_count}")


def _print_summary_plain(
    all_passed: bool,
    total_steps: int,
    passed_steps: int,
    total_findings: int,
    duration: float,
    cost: float,
    run_id: str,
) -> None:
    """Print a plain-text summary for CI/screen-reader contexts."""
    verdict = "PASSED" if all_passed else "FAILED"
    _plain_print(
        f"RESULT: {verdict} -- {passed_steps}/{total_steps} steps passed, "
        f"{total_findings} findings, {duration:.1f}s, ${cost:.4f}"
    )
    _plain_print(f"Run ID: {run_id}")


# ── JUnit XML writer ──────────────────────────────────────────────────────


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
    testsuite.set("name", f"specterqa-{product}")
    testsuite.set("tests", str(len(step_reports)))
    testsuite.set("time", f"{duration:.2f}")

    failures = 0
    for step in step_reports:
        testcase = ET.SubElement(testsuite, "testcase")
        testcase.set("name", step.step_id)
        testcase.set("classname", f"specterqa.{product}")
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


# ── Main command ──────────────────────────────────────────────────────────


def run(
    product: str = typer.Option(
        ...,
        "--product",
        "-p",
        help="Product name (must match a .yaml in .specterqa/products/). [required]",
    ),
    journey: str | None = typer.Option(
        None,
        "--journey",
        "-j",
        help="Specific journey/scenario ID to run. Default: all journeys for the product.",
    ),
    level: str = typer.Option(
        "standard",
        "--level",
        "-l",
        help="Test level: smoke, standard, or thorough.  [default: standard]",
    ),
    viewport: str = typer.Option(
        "1280x720",
        "--viewport",
        help="Browser viewport as WIDTHxHEIGHT.  [default: 1280x720]",
    ),
    budget: float = typer.Option(
        _resolve_budget_default(),
        "--budget",
        "-b",
        help=(
            "Maximum budget for this run in USD. "
            "Falls back to SPECTERQA_BUDGET env var if set.  [default: 5.0]"
        ),
    ),
    fail_on_severity: str = typer.Option(
        "any",
        "--fail-on-severity",
        help=(
            "Exit 1 only if findings meet or exceed this severity threshold. "
            "Choices: block, critical, high, medium, low, any.  [default: any]"
        ),
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        help=(
            "Use plain ASCII output — no Rich formatting, colors, or Unicode. "
            "Uses [PASS]/[FAIL]/[SKIP] labels. For screen readers and minimal terminals."
        ),
    ),
    junit_xml: Path | None = typer.Option(
        None,
        "--junit-xml",
        help="Path to write JUnit XML report (for CI integration).",
    ),
    output_format: str = typer.Option(
        "text",
        "--output",
        "-o",
        help="Output format: text or json.  [default: text]",
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
    """Run SpecterQA behavioral tests against a product.

    Launches AI personas that navigate your app via real browser sessions,
    evaluating UX, functionality, and error handling through vision-based
    interaction.

    \b
    Examples:
      specterqa run -p demo
      specterqa run -p myapp --journey onboarding-happy-path
      specterqa run -p myapp --level smoke --budget 1.00
      specterqa run -p myapp --fail-on-severity high   # ignore low/medium findings
      specterqa run -p myapp --output json | jq '.passed'
      specterqa run -p myapp --plain                   # CI-safe ASCII output
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s  %(message)s")

    # Detect non-TTY context (CI/pipe) — auto-enable plain mode when not interactive.
    # This prevents spinner \r overwrites from corrupting CI log files.
    is_tty = sys.stdout.isatty()
    if not is_tty and not plain and output_format != "json":
        plain = True

    # Validate level
    valid_levels = {"smoke", "standard", "thorough"}
    if level not in valid_levels:
        _print_error(
            console,
            plain,
            f"Invalid level: {level}\n\nValid levels: {', '.join(sorted(valid_levels))}",
            "Config Error",
        )
        raise typer.Exit(code=2)

    # Validate fail-on-severity
    if fail_on_severity not in _SEVERITY_LEVELS:
        _print_error(
            console,
            plain,
            f"Invalid --fail-on-severity: {fail_on_severity!r}\n\nValid choices: {', '.join(_SEVERITY_LEVELS)}",
            "Config Error",
        )
        raise typer.Exit(code=2)

    # Validate output format
    if output_format not in ("text", "json"):
        _print_error(
            console,
            plain,
            f"Invalid output format: {output_format!r}\n\nValid formats: text, json",
            "Config Error",
        )
        raise typer.Exit(code=2)

    # Parse viewport
    vp = _parse_viewport(viewport, plain)

    # Resolve project directory
    project_dir = _resolve_project_dir()

    # Resolve API key
    try:
        api_key = resolve_api_key(project_dir)
    except SpecterQAConfigError as exc:
        _print_error(console, plain, str(exc), "API Key Error")
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
    except SpecterQAConfigError as exc:
        _print_error(console, plain, str(exc), "Config Error")
        raise typer.Exit(code=2)

    # Print header
    if output_format == "text":
        if plain:
            _print_run_header_plain(
                product=product,
                journey=journey,
                level=level,
                viewport=vp,
                budget=budget,
                headless=headless,
                fail_on_severity=fail_on_severity,
            )
        else:
            _print_run_header_rich(
                product=product,
                journey=journey,
                level=level,
                viewport=vp,
                budget=budget,
                headless=headless,
                api_key_display=mask_key(api_key),
                fail_on_severity=fail_on_severity,
            )

    # Import orchestrator (may fail if playwright not installed)
    try:
        from specterqa.engine.orchestrator import SpecterQAOrchestrator
    except ImportError as exc:
        _print_error(
            console,
            plain,
            f"Failed to import SpecterQA engine: {exc}\n\n"
            "This usually means a dependency is missing.\n"
            "Try: pip install specterqa\n"
            "Then: specterqa install",
            "Import Error",
        )
        raise typer.Exit(code=3)

    # Create orchestrator and run
    orchestrator = SpecterQAOrchestrator(config)

    start_time = time.monotonic()

    if output_format == "text":
        if plain:
            _plain_print("Running tests...")
        else:
            console.print("[bold]Running tests...[/bold]\n")

    try:
        report_md, all_passed = orchestrator.run(
            product=product,
            scenario_id=journey,
            level=level,
            viewport=viewport if viewport != "1280x720" else None,
        )
    except KeyboardInterrupt:
        if plain:
            _plain_print("Run interrupted by user.")
        else:
            console.print("\n[yellow]Run interrupted by user.[/yellow]")
        raise typer.Exit(code=1)
    except SpecterQAConfigError as exc:
        _print_error(console, plain, str(exc), "Config Error")
        raise typer.Exit(code=2)
    except Exception as exc:
        logger.exception("Unexpected error during run")
        _print_error(
            console,
            plain,
            f"Unexpected error: {exc}\n\nRun with --verbose for full traceback.",
            "Infrastructure Error",
        )
        raise typer.Exit(code=3)

    duration = time.monotonic() - start_time

    # Load structured result from the evidence directory
    run_result_data = _load_latest_run_result(config.evidence_dir)

    if output_format == "json":
        # JSON output mode — dump the run result or a minimal structure
        if run_result_data:
            output_console.print(json.dumps(run_result_data, indent=2))
        else:
            output_console.print(
                json.dumps(
                    {
                        "passed": all_passed,
                        "report": report_md,
                        "duration_seconds": round(duration, 2),
                    },
                    indent=2,
                )
            )
    else:
        # Text output mode
        if run_result_data:
            _print_structured_results(run_result_data, duration, plain)
        else:
            # Fallback: print the markdown report
            if plain:
                _plain_print(report_md)
            else:
                console.print()
                console.print(report_md)
                console.print()

    # Write JUnit XML if requested
    if junit_xml and run_result_data:
        try:
            from specterqa.engine.report_generator import StepReport

            step_reports = []
            for sr in run_result_data.get("step_reports", []):
                step_reports.append(
                    StepReport(
                        step_id=sr.get("step_id", "unknown"),
                        description=sr.get("description", ""),
                        mode=sr.get("mode", ""),
                        passed=sr.get("passed", False),
                        duration_seconds=sr.get("duration_seconds", 0),
                        error=sr.get("error"),
                    )
                )
            _write_junit_xml(junit_xml, run_result_data.get("run_id", "unknown"), product, step_reports, duration)
            if output_format == "text":
                if plain:
                    _plain_print(f"JUnit XML written to: {junit_xml}")
                else:
                    console.print(f"[dim]JUnit XML written to: {junit_xml}[/dim]\n")
        except Exception as exc:
            if plain:
                _plain_print(f"Warning: Failed to write JUnit XML: {exc}")
            else:
                console.print(f"[yellow]Warning: Failed to write JUnit XML: {exc}[/yellow]")

    # Exit code: apply --fail-on-severity filter
    # 1. Determine if the run had any step failures
    # 2. Check if any finding meets the severity threshold
    if not all_passed:
        findings = run_result_data.get("findings", []) if run_result_data else []
        qualifying_findings = [f for f in findings if _finding_meets_threshold(f.get("severity", ""), fail_on_severity)]
        if qualifying_findings or fail_on_severity == "any":
            raise typer.Exit(code=1)
        # No qualifying findings — exit 0 despite test failures
        if output_format == "text":
            msg = (
                f"Tests failed but no findings at or above '{fail_on_severity}' severity. "
                "Exiting 0 per --fail-on-severity."
            )
            if plain:
                _plain_print(f"NOTE: {msg}")
            else:
                console.print(f"[dim]{msg}[/dim]")


# ── Result loading ────────────────────────────────────────────────────────


def _load_latest_run_result(evidence_dir: Path) -> dict | None:
    """Load the most recent run-result.json from the evidence directory."""
    if not evidence_dir.is_dir():
        return None

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


# ── Structured output renderers ───────────────────────────────────────────


def _print_structured_results(data: dict, duration: float, plain: bool = False) -> None:
    """Print structured results from a run-result.json dict."""
    step_reports = data.get("step_reports", [])
    # Sort findings by severity (most severe first)
    findings = _sort_findings_by_severity(data.get("findings", []))
    total_steps = len(step_reports)
    passed_steps = sum(1 for s in step_reports if s.get("passed"))
    run_id = data.get("run_id", "unknown")
    cost = data.get("cost_usd", 0.0)
    all_passed = data.get("passed", False)

    # Print each step result
    for i, step in enumerate(step_reports, 1):
        step_findings = [f for f in findings if f.get("step_id") == step.get("step_id")]
        if plain:
            _print_step_result_plain(
                step_num=i,
                total_steps=total_steps,
                description=step.get("description", ""),
                passed=step.get("passed", False),
                duration=step.get("duration_seconds", 0),
                error=step.get("error"),
                findings_count=len(step_findings),
            )
        else:
            _print_step_result_rich(
                step_num=i,
                total_steps=total_steps,
                description=step.get("description", ""),
                passed=step.get("passed", False),
                duration=step.get("duration_seconds", 0),
                error=step.get("error"),
                findings_count=len(step_findings),
            )

    # Print findings
    if findings:
        if plain:
            _plain_print("")
            _plain_print(f"Findings ({len(findings)}):")
            for f in findings:
                severity = f.get("severity", "?").upper()
                category = f.get("category", "?")
                desc = f.get("description", "")
                step_id = f.get("step_id", "?")
                _plain_print(f"  [{severity}] ({category}) {desc}  [step: {step_id}]")
        else:
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

    # Print summary
    if plain:
        _plain_print("")
        _print_summary_plain(
            all_passed=all_passed,
            total_steps=total_steps,
            passed_steps=passed_steps,
            total_findings=len(findings),
            duration=duration,
            cost=cost,
            run_id=run_id,
        )
    else:
        _print_summary_panel_rich(
            all_passed=all_passed,
            total_steps=total_steps,
            passed_steps=passed_steps,
            total_findings=len(findings),
            duration=duration,
            cost=cost,
            run_id=run_id,
        )
