"""SpecterQA CLI — Main Typer entry point.

Registers all subcommands and provides --version / --verbose global options.
"""

from __future__ import annotations

import typer
from rich.console import Console

from specterqa import __version__

# ── ASCII Banner ──────────────────────────────────────────────────────────

BANNER = r"""
███████╗██████╗ ███████╗ ██████╗████████╗███████╗██████╗  ██████╗  █████╗
██╔════╝██╔══██╗██╔════╝██╔════╝╚══██╔══╝██╔════╝██╔══██╗██╔═══██╗██╔══██╗
███████╗██████╔╝█████╗  ██║        ██║   █████╗  ██████╔╝██║   ██║███████║
╚════██║██╔═══╝ ██╔══╝  ██║        ██║   ██╔══╝  ██╔══██╗██║▄▄ ██║██╔══██║
███████║██║     ███████╗╚██████╗   ██║   ███████╗██║  ██║╚██████╔╝██║  ██║
╚══════╝╚═╝     ╚══════╝ ╚═════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝ ╚══▀▀═╝ ╚═╝  ╚═╝
"""

TAGLINE = "AI personas walk your app so real users don't trip."

console = Console()

# ── Version callback ──────────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        console.print(BANNER, style="bold cyan")
        console.print(f"  {TAGLINE}", style="dim")
        console.print(f"  v{__version__}\n", style="bold")
        raise typer.Exit()


# ── Main app ──────────────────────────────────────────────────────────────

app = typer.Typer(
    name="specterqa",
    help=f"{BANNER}\n{TAGLINE}",
    rich_markup_mode="rich",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        help="Show SpecterQA version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output.",
    ),
) -> None:
    """SpecterQA -- AI persona-based behavioral testing for web apps.

    No test scripts. YAML-configured. Vision-powered.
    """
    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG, format="%(name)s  %(message)s")


# ── Register subcommands ──────────────────────────────────────────────────
# Each subcommand is a separate module to keep this file lean.

from specterqa.cli.config_cmd import config_app  # noqa: E402
from specterqa.cli.dashboard import dashboard  # noqa: E402
from specterqa.cli.init_cmd import init  # noqa: E402
from specterqa.cli.install import install  # noqa: E402
from specterqa.cli.report import report  # noqa: E402
from specterqa.cli.run import run  # noqa: E402
from specterqa.cli.validate import validate  # noqa: E402

app.command(name="init", help="Initialize a .specterqa/ project directory.")(init)
app.command(name="install", help="Install browser dependencies (Playwright).")(install)
app.command(name="run", help="Run SpecterQA behavioral tests.")(run)
app.command(name="report", help="View or export run reports.")(report)
app.command(name="validate", help="Validate YAML config without executing tests (zero cost).")(validate)
app.command(name="dashboard", help="Launch the evidence dashboard viewer.")(dashboard)
app.add_typer(config_app, name="config", help="View and manage SpecterQA configuration.")
