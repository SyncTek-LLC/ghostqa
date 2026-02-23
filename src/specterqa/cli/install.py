"""specterqa install — Install browser dependencies (Playwright).

Runs `playwright install` with a Rich progress spinner and validates
the installation succeeded.
"""

from __future__ import annotations

import subprocess
import sys

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


def install(
    browsers: str = typer.Option(
        "chromium",
        "--browsers",
        "-b",
        help="Browsers to install (comma-separated). Options: chromium, firefox, webkit.",
    ),
    ci: bool = typer.Option(
        False,
        "--ci",
        help="Silent mode for CI environments (suppress interactive output).",
    ),
) -> None:
    """Install Playwright browsers required for SpecterQA browser testing.

    By default installs Chromium only. Use --browsers to specify others.
    """
    browser_list = [b.strip() for b in browsers.split(",") if b.strip()]

    if not ci:
        console.print()
        console.print(
            Panel(
                f"Installing browsers: [bold cyan]{', '.join(browser_list)}[/bold cyan]\n\n"
                "This downloads browser binaries via Playwright.\n"
                "First run may take a few minutes.",
                title="[bold]SpecterQA Browser Setup[/bold]",
                border_style="blue",
            )
        )
        console.print()

    # Build command
    cmd = [sys.executable, "-m", "playwright", "install"] + browser_list

    try:
        if ci:
            # Silent mode — capture all output
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                console.print(f"[red]Installation failed (exit {result.returncode})[/red]")
                if result.stderr:
                    console.print(f"[dim]{result.stderr.strip()}[/dim]")
                raise typer.Exit(code=3)
        else:
            # Interactive mode — show progress with spinner
            with console.status(
                f"[bold blue]Installing {', '.join(browser_list)}...[/bold blue]",
                spinner="dots",
            ):
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )

            if result.returncode != 0:
                console.print(
                    Panel(
                        f"[red]Playwright install failed (exit code {result.returncode}).[/red]\n\n"
                        f"{result.stderr.strip() if result.stderr else 'No error output.'}\n\n"
                        "[dim]Try running manually:[/dim]\n"
                        f"  {' '.join(cmd)}",
                        title="[red]Installation Failed[/red]",
                        border_style="red",
                    )
                )
                raise typer.Exit(code=3)

            # Show any output from playwright (often lists installed paths)
            if result.stdout and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    console.print(f"  [dim]{line}[/dim]")
                console.print()

    except subprocess.TimeoutExpired:
        console.print(
            Panel(
                "[red]Installation timed out after 10 minutes.[/red]\n\nCheck your network connection and try again.",
                title="[red]Timeout[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=3)

    except FileNotFoundError:
        console.print(
            Panel(
                "[red]Playwright is not installed.[/red]\n\nInstall it first:\n  pip install playwright",
                title="[red]Missing Dependency[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=3)

    if not ci:
        console.print(
            Panel(
                f"[green]Successfully installed: {', '.join(browser_list)}[/green]\n\n"
                "You're ready to run SpecterQA:\n"
                "  [bold]specterqa run --product demo[/bold]",
                title="[bold green]Installation Complete[/bold green]",
                border_style="green",
            )
        )
