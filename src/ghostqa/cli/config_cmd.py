"""ghostqa config — View and manage GhostQA configuration.

Subcommands: show, set, set-key.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ghostqa.config import GhostQAConfig, GhostQAConfigError
from ghostqa.credentials import mask_key, resolve_api_key

console = Console()

config_app = typer.Typer(
    name="config",
    help="View and manage GhostQA configuration.",
    no_args_is_help=True,
)


def _find_project_dir() -> Path:
    """Locate the .ghostqa/ project directory by searching upward from cwd."""
    current = Path.cwd()
    for base in [current, *current.parents]:
        candidate = base / ".ghostqa"
        if candidate.is_dir():
            return candidate
    return current / ".ghostqa"


def _load_raw_config(project_dir: Path) -> dict:
    """Load the raw YAML config dict."""
    config_path = project_dir / "config.yaml"
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _save_raw_config(project_dir: Path, data: dict) -> None:
    """Write the config dict to config.yaml."""
    config_path = project_dir / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


@config_app.command(name="show")
def config_show(
    dir: Path | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="Path to .ghostqa/ directory.",
    ),
) -> None:
    """Show the resolved GhostQA configuration.

    Displays all effective config values, merging config.yaml with
    defaults. API keys are masked for safety.
    """
    project_dir = dir or _find_project_dir()
    config_path = project_dir / "config.yaml"

    # Load resolved config
    try:
        if config_path.is_file():
            config = GhostQAConfig.from_file(config_path)
        else:
            config = GhostQAConfig()
            config.project_dir = project_dir
    except GhostQAConfigError as exc:
        console.print(Panel(f"[red]{exc}[/red]", title="[red]Config Error[/red]", border_style="red"))
        raise typer.Exit(code=2)

    # Resolve API key status
    try:
        api_key = resolve_api_key(project_dir)
        key_display = mask_key(api_key)
        key_source = _identify_key_source(project_dir)
    except GhostQAConfigError:
        key_display = "[red]NOT SET[/red]"
        key_source = "-"

    # Build table
    table = Table(title="GhostQA Configuration", border_style="cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    table.add_column("Source", style="dim")

    table.add_row("Project Dir", str(project_dir), "resolved")
    table.add_row("Config File", str(config_path), "exists" if config_path.is_file() else "missing")
    table.add_row("Products Dir", str(config.products_dir), "config")
    table.add_row("Personas Dir", str(config.personas_dir), "config")
    table.add_row("Journeys Dir", str(config.journeys_dir), "config")
    table.add_row("Evidence Dir", str(config.evidence_dir), "config")
    table.add_row("", "", "")
    table.add_row("API Key", key_display, key_source)
    table.add_row("Budget", f"${config.budget:.2f}", "config")
    table.add_row("Headless", str(config.headless), "config")
    table.add_row("Viewport", f"{config.viewport[0]}x{config.viewport[1]}", "config")
    table.add_row("Timeout", f"{config.timeout}s", "config")
    table.add_row("Level", config.level, "config")

    console.print()
    console.print(table)
    console.print()


def _identify_key_source(project_dir: Path) -> str:
    """Determine where the API key is coming from."""
    import os

    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env: ANTHROPIC_API_KEY"

    env_path = Path(".env")
    if env_path.exists():
        try:
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith("ANTHROPIC_API_KEY"):
                        return ".env file"
        except Exception:
            pass

    config_path = project_dir / "config.yaml"
    if config_path.is_file():
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            if data.get("anthropic_api_key") or data.get("api_key"):
                return "config.yaml"
        except Exception:
            pass

    global_config = Path.home() / ".ghostqa" / "config.yaml"
    if global_config.is_file():
        return "~/.ghostqa/config.yaml"

    return "unknown"


@config_app.command(name="set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key to set."),
    value: str = typer.Argument(..., help="Value to set."),
    dir: Path | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="Path to .ghostqa/ directory.",
    ),
) -> None:
    """Set a configuration value in .ghostqa/config.yaml.

    Examples:
      ghostqa config set budget 10.00
      ghostqa config set headless false
      ghostqa config set timeout 900
    """
    project_dir = dir or _find_project_dir()
    data = _load_raw_config(project_dir)

    # Type coercion for known numeric/boolean keys
    coerced_value: object = value
    if key in ("budget",):
        try:
            coerced_value = float(value)
        except ValueError:
            console.print(f"[red]Invalid float value for '{key}':[/red] {value}")
            raise typer.Exit(code=2)
    elif key in ("timeout",):
        try:
            coerced_value = int(value)
        except ValueError:
            console.print(f"[red]Invalid integer value for '{key}':[/red] {value}")
            raise typer.Exit(code=2)
    elif key in ("headless",):
        coerced_value = value.lower() in ("true", "1", "yes")

    data[key] = coerced_value
    _save_raw_config(project_dir, data)

    console.print(f"[green]Set[/green] {key} = {coerced_value} [dim]in {project_dir / 'config.yaml'}[/dim]")


@config_app.command(name="set-key")
def config_set_key(
    dir: Path | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="Path to .ghostqa/ directory.",
    ),
    global_config: bool = typer.Option(
        False,
        "--global",
        "-g",
        help="Save to global config (~/.ghostqa/config.yaml) instead of project config.",
    ),
) -> None:
    """Interactively set the Anthropic API key.

    Prompts for the key and saves it to the project or global config.
    The key is never displayed in full after entry.
    """
    api_key = typer.prompt(
        "Enter your Anthropic API key",
        hide_input=True,
    )

    if not api_key.strip():
        console.print("[red]API key cannot be empty.[/red]")
        raise typer.Exit(code=2)

    api_key = api_key.strip()

    if global_config:
        target_dir = Path.home() / ".ghostqa"
    else:
        target_dir = dir or _find_project_dir()

    data = _load_raw_config(target_dir)
    data["anthropic_api_key"] = api_key
    _save_raw_config(target_dir, data)

    config_path = target_dir / "config.yaml"
    console.print(f"\n[green]API key saved[/green] ({mask_key(api_key)}) [dim]to {config_path}[/dim]")
    console.print(
        "\n[dim]Tip: The ANTHROPIC_API_KEY environment variable takes priority over config file values.[/dim]"
    )
