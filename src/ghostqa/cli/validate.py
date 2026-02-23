"""ghostqa validate — Parse and validate YAML config without executing tests.

Parses product, persona, and journey YAML files and reports any errors
without making any API calls. Zero cost. Use this to catch configuration
mistakes before committing to a real run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

console = Console(stderr=True)

# ── Severity ordering ─────────────────────────────────────────────────────

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _sev_style(severity: str) -> str:
    return {"error": "bold red", "warning": "yellow", "info": "dim"}.get(severity, "")


# ── Validation helpers ────────────────────────────────────────────────────


def _validate_product(path: Path) -> list[dict[str, Any]]:
    """Validate a product YAML file. Returns list of issue dicts."""
    issues: list[dict[str, Any]] = []

    try:
        import yaml  # type: ignore
    except ImportError:
        issues.append({"severity": "error", "field": "", "message": "PyYAML not installed. Run: pip install pyyaml"})
        return issues

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        issues.append({"severity": "error", "field": "yaml_syntax", "message": f"YAML parse error: {exc}"})
        return issues

    if not isinstance(data, dict):
        issues.append({"severity": "error", "field": "root", "message": "Product file must be a YAML mapping"})
        return issues
    # JSON Schema validation (for editor/tooling consistency)
    try:
        import json
        from jsonschema import Draft7Validator  # type: ignore

        schema_path = Path(__file__).resolve().parents[3] / "schemas" / "product.schema.json"
        if schema_path.is_file():
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator = Draft7Validator(schema)

            schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
            for err in schema_errors:
                loc = ".".join(str(p) for p in err.path) if err.path else "root"
                issues.append(
                    {
                        "severity": "error",
                        "field": f"schema.{loc}",
                        "message": f"Schema validation error: {err.message}",
                    }
                )
    except Exception:
        # If schema validation isn't available for some reason, don't block validate entirely.
        pass

    product = data.get("product", data)

    # Required fields
    if not product.get("name"):
        issues.append({"severity": "error", "field": "product.name", "message": "Missing required field: product.name"})

    # Check base URL or services
    services = product.get("services", {})
    frontend_url = services.get("frontend", {}).get("url", "") if isinstance(services, dict) else ""
    base_url = product.get("base_url", "")
    if not frontend_url and not base_url:
        issues.append(
            {
                "severity": "warning",
                "field": "product.services.frontend.url",
                "message": "No base URL found. Set services.frontend.url or product.base_url",
            }
        )

    # Check cost limits are positive numbers
    cost_limits = product.get("cost_limits", {})
    if isinstance(cost_limits, dict):
        per_run = cost_limits.get("per_run_usd")
        if per_run is not None and (not isinstance(per_run, (int, float)) or per_run <= 0):
            issues.append(
                {
                    "severity": "error",
                    "field": "product.cost_limits.per_run_usd",
                    "message": f"per_run_usd must be a positive number, got: {per_run!r}",
                }
            )

    # Viewports check
    viewports = product.get("viewports", {})
    if isinstance(viewports, dict):
        for vp_name, vp_cfg in viewports.items():
            if not isinstance(vp_cfg, dict):
                continue
            for dim in ("width", "height"):
                val = vp_cfg.get(dim)
                if val is None:
                    issues.append(
                        {
                            "severity": "warning",
                            "field": f"product.viewports.{vp_name}.{dim}",
                            "message": f"Viewport '{vp_name}' missing '{dim}'",
                        }
                    )
                elif not isinstance(val, int) or val <= 0:
                    issues.append(
                        {
                            "severity": "error",
                            "field": f"product.viewports.{vp_name}.{dim}",
                            "message": f"Viewport '{vp_name}'.{dim} must be a positive integer, got: {val!r}",
                        }
                    )

    return issues


def _validate_persona(path: Path) -> list[dict[str, Any]]:
    """Validate a persona YAML file. Returns list of issue dicts."""
    issues: list[dict[str, Any]] = []

    try:
        import yaml  # type: ignore
    except ImportError:
        return [{"severity": "error", "field": "", "message": "PyYAML not installed"}]

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        issues.append({"severity": "error", "field": "yaml_syntax", "message": f"YAML parse error: {exc}"})
        return issues

    if not isinstance(data, dict):
        issues.append({"severity": "error", "field": "root", "message": "Persona file must be a YAML mapping"})
        return issues

    persona = data.get("persona", data)

    if not persona.get("name"):
        issues.append({"severity": "error", "field": "persona.name", "message": "Missing required field: persona.name"})

    if not persona.get("goals"):
        issues.append(
            {
                "severity": "warning",
                "field": "persona.goals",
                "message": "persona.goals is empty — persona will have no behavioral guidance",
            }
        )

    # credentials is optional but often referenced in journeys
    credentials = persona.get("credentials", {})
    if credentials and not isinstance(credentials, dict):
        issues.append(
            {
                "severity": "error",
                "field": "persona.credentials",
                "message": "persona.credentials must be a mapping (key: value pairs)",
            }
        )

    return issues


def _validate_journey(path: Path, personas_dir: Path | None = None) -> list[dict[str, Any]]:
    """Validate a journey YAML file. Returns list of issue dicts."""
    issues: list[dict[str, Any]] = []

    try:
        import yaml  # type: ignore
    except ImportError:
        return [{"severity": "error", "field": "", "message": "PyYAML not installed"}]

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        issues.append({"severity": "error", "field": "yaml_syntax", "message": f"YAML parse error: {exc}"})
        return issues

    if not isinstance(data, dict):
        issues.append({"severity": "error", "field": "root", "message": "Journey file must be a YAML mapping"})
        return issues

    scenario = data.get("scenario", data)

    # Required fields — accept both `id` and `scenario.id`
    scenario_id = scenario.get("id") or scenario.get("scenario_id") or (data != scenario and data.get("id"))
    if not scenario_id:
        issues.append(
            {
                "severity": "error",
                "field": "id",
                "message": "Missing required field: id (scenario identifier)",
            }
        )

    # name / display_name are optional but nice to have
    has_name = scenario.get("name") or scenario.get("display_name") or (data != scenario and data.get("display_name"))
    if not has_name:
        issues.append(
            {
                "severity": "warning",
                "field": "name",
                "message": "name/display_name is missing — consider adding a human-readable name",
            }
        )

    # Steps — accept both 'steps' key
    steps = scenario.get("steps", [])
    if not steps:
        issues.append(
            {
                "severity": "error",
                "field": "steps",
                "message": "Journey has no steps — at least one step is required",
            }
        )
    else:
        seen_step_ids: set[str] = set()
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                issues.append(
                    {
                        "severity": "error",
                        "field": f"steps[{i}]",
                        "message": f"Step {i} is not a mapping",
                    }
                )
                continue

            step_id = step.get("id", "")
            if not step_id:
                issues.append(
                    {
                        "severity": "error",
                        "field": f"steps[{i}].id",
                        "message": f"Step {i} is missing required field: id",
                    }
                )
            elif step_id in seen_step_ids:
                issues.append(
                    {
                        "severity": "error",
                        "field": f"steps[{i}].id",
                        "message": f"Duplicate step id: '{step_id}'",
                    }
                )
            else:
                seen_step_ids.add(step_id)

            # Accept both `mode` and `type` keys (two schemas coexist in the codebase)
            mode = step.get("mode") or step.get("type") or ""
            valid_modes = {"api", "browser", "native_app", "ios_simulator"}
            if mode and mode not in valid_modes:
                issues.append(
                    {
                        "severity": "error",
                        "field": f"steps[{i}].mode",
                        "message": f"Unknown step mode: '{mode}'. Valid modes: {', '.join(sorted(valid_modes))}",
                    }
                )

            if not step.get("goal") and not step.get("description"):
                issues.append(
                    {
                        "severity": "warning",
                        "field": f"steps[{i}].goal",
                        "message": f"Step '{step_id or i}' has no goal or description — AI will have no guidance",
                    }
                )

    # Personas — accept both `personas: [{ref: ...}]` and flat `persona: name` string
    persona_refs = scenario.get("personas", [])
    flat_persona = scenario.get("persona")  # Flat string format used by some schemas
    if not persona_refs and not flat_persona:
        issues.append(
            {
                "severity": "error",
                "field": "personas",
                "message": "Journey must reference at least one persona",
            }
        )
    elif flat_persona and isinstance(flat_persona, str):
        # Flat string format: `persona: alex_developer`
        if personas_dir is not None:
            persona_path = personas_dir / f"{flat_persona}.yaml"
            if not persona_path.is_file():
                issues.append(
                    {
                        "severity": "error",
                        "field": "persona",
                        "message": f"Referenced persona '{flat_persona}' not found at {persona_path}",
                    }
                )
    elif persona_refs:
        for pref in persona_refs:
            if isinstance(pref, dict):
                ref_name = pref.get("ref", "")
                if not ref_name:
                    issues.append(
                        {
                            "severity": "error",
                            "field": "personas[].ref",
                            "message": "Persona reference missing 'ref' field",
                        }
                    )
                elif personas_dir is not None:
                    # Check that the referenced persona file exists
                    persona_path = personas_dir / f"{ref_name}.yaml"
                    if not persona_path.is_file():
                        issues.append(
                            {
                                "severity": "error",
                                "field": "personas[].ref",
                                "message": f"Referenced persona '{ref_name}' not found at {persona_path}",
                            }
                        )

    # Tags should be a list
    tags = scenario.get("tags")
    if tags is not None and not isinstance(tags, list):
        issues.append(
            {
                "severity": "warning",
                "field": "tags",
                "message": f"tags should be a list, got: {type(tags).__name__}",
            }
        )

    return issues


def _resolve_project_dir() -> Path:
    """Find the .ghostqa/ project directory, searching upward from cwd."""
    current = Path.cwd()
    candidate = current / ".ghostqa"
    if candidate.is_dir():
        return candidate
    for parent in current.parents:
        candidate = parent / ".ghostqa"
        if candidate.is_dir():
            return candidate
    return current / ".ghostqa"


# ── CLI command ───────────────────────────────────────────────────────────


def validate(
    product: str | None = typer.Option(
        None,
        "--product",
        "-p",
        help="Validate a specific product (and its journeys). Omit to validate all products.",
    ),
    dir: Path | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="GhostQA project directory. Defaults to auto-detected .ghostqa/ from cwd.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit 1 on warnings as well as errors (default: exit 1 on errors only).",
    ),
) -> None:
    """Validate product, persona, and journey YAML files without executing tests.

    Parses all configuration files and reports errors and warnings without
    making any API calls. Safe to run at any time — zero cost.

    \b
    Examples:
      ghostqa validate                        # Validate all products
      ghostqa validate -p demo               # Validate 'demo' product only
      ghostqa validate --strict              # Fail on warnings too
    """
    # Resolve project directory
    if dir is not None:
        project_dir = dir.resolve()
        if not project_dir.name == ".ghostqa":
            project_dir = project_dir / ".ghostqa"
    else:
        project_dir = _resolve_project_dir()

    if not project_dir.is_dir():
        console.print(
            Panel(
                f"[red]Project not initialized.[/red]\n\n"
                f"Looked for .ghostqa/ in: {project_dir.parent}\n\n"
                "Fix: [bold]ghostqa init[/bold]",
                title="[red]Not Initialized[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    products_dir = project_dir / "products"
    personas_dir = project_dir / "personas"
    journeys_dir = project_dir / "journeys"

    total_errors = 0
    total_warnings = 0
    any_files_found = False

    # ── Validate products ──────────────────────────────────────────────
    product_files: list[Path] = []
    if products_dir.is_dir():
        if product:
            # Single product
            candidate = products_dir / f"{product}.yaml"
            if candidate.is_file():
                product_files.append(candidate)
            else:
                # Check directory-style
                dir_candidate = products_dir / product / "_product.yaml"
                if dir_candidate.is_file():
                    product_files.append(dir_candidate)
                else:
                    console.print(
                        Panel(
                            f"[red]Product '{product}' not found.[/red]\n\n"
                            f"Looked in:\n"
                            f"  {candidate}\n"
                            f"  {dir_candidate}\n\n"
                            "Run [bold]ghostqa list products[/bold] to see available products.",
                            title="[red]Product Not Found[/red]",
                            border_style="red",
                        )
                    )
                    raise typer.Exit(code=2)
        else:
            product_files = sorted(products_dir.glob("*.yaml"))
            # Also check directory-style products
            for subdir in sorted(products_dir.iterdir()):
                if subdir.is_dir():
                    candidate = subdir / "_product.yaml"
                    if candidate.is_file():
                        product_files.append(candidate)

    for pf in product_files:
        any_files_found = True
        issues = _validate_product(pf)
        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]
        total_errors += len(errors)
        total_warnings += len(warnings)

        _print_file_result(pf, issues, project_dir)

    # ── Validate personas ──────────────────────────────────────────────
    if personas_dir.is_dir():
        for pf in sorted(personas_dir.glob("*.yaml")):
            any_files_found = True
            issues = _validate_persona(pf)
            errors = [i for i in issues if i["severity"] == "error"]
            warnings = [i for i in issues if i["severity"] == "warning"]
            total_errors += len(errors)
            total_warnings += len(warnings)

            _print_file_result(pf, issues, project_dir)

    # ── Validate journeys ──────────────────────────────────────────────
    journey_files: list[Path] = []
    if journeys_dir.is_dir():
        journey_files = sorted(journeys_dir.glob("*.yaml"))

    for jf in journey_files:
        any_files_found = True
        issues = _validate_journey(jf, personas_dir=personas_dir if personas_dir.is_dir() else None)
        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]
        total_errors += len(errors)
        total_warnings += len(warnings)

        _print_file_result(jf, issues, project_dir)

    if not any_files_found:
        console.print(
            Panel(
                "[yellow]No YAML files found to validate.[/yellow]\n\n"
                f"Looked in: {project_dir}\n\n"
                "Run [bold]ghostqa init[/bold] to scaffold sample files.",
                title="No Files Found",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=0)

    # ── Summary ────────────────────────────────────────────────────────
    console.print()
    if total_errors == 0 and total_warnings == 0:
        console.print(Panel("[bold green]All files valid. No errors or warnings.[/bold green]", border_style="green"))
    elif total_errors > 0:
        console.print(
            Panel(
                f"[bold red]Validation failed.[/bold red]  "
                f"{total_errors} error(s), {total_warnings} warning(s)\n\n"
                "Fix the errors above before running tests.",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)
    else:
        # Warnings only
        if strict:
            console.print(
                Panel(
                    f"[bold yellow]Validation warnings found (--strict mode).[/bold yellow]  "
                    f"{total_warnings} warning(s)",
                    border_style="yellow",
                )
            )
            raise typer.Exit(code=1)
        else:
            console.print(
                Panel(
                    f"[yellow]Validation passed with {total_warnings} warning(s).[/yellow]  "
                    "Use [bold]--strict[/bold] to fail on warnings.",
                    border_style="yellow",
                )
            )


def _print_file_result(path: Path, issues: list[dict[str, Any]], project_dir: Path) -> None:
    """Print validation results for a single file."""
    try:
        display_path = path.relative_to(project_dir.parent)
    except ValueError:
        display_path = path  # type: ignore[assignment]

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if not issues:
        console.print(f"  [green]✓[/green] [dim]{display_path}[/dim]  [green]OK[/green]")
        return

    if errors:
        status = f"[bold red]{len(errors)} error(s)[/bold red]"
        if warnings:
            status += f", [yellow]{len(warnings)} warning(s)[/yellow]"
        console.print(f"  [red]✗[/red] [bold]{display_path}[/bold]  {status}")
    else:
        console.print(f"  [yellow]![/yellow] [dim]{display_path}[/dim]  [yellow]{len(warnings)} warning(s)[/yellow]")

    for issue in sorted(issues, key=lambda i: _SEVERITY_ORDER.get(i["severity"], 99)):
        sev = issue["severity"]
        field = issue.get("field", "")
        msg = issue["message"]
        sev_label = {
            "error": "[bold red]ERROR[/bold red]",
            "warning": "[yellow]WARN[/yellow]",
            "info": "[dim]INFO[/dim]",
        }.get(sev, sev)
        field_str = f"[dim] ({field})[/dim]" if field else ""
        console.print(f"      {sev_label}{field_str}  {msg}")
