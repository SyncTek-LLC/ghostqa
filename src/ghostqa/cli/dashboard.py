"""ghostqa dashboard — Launch the evidence dashboard viewer.

Starts a local web server that serves the GhostQA evidence viewer,
providing a visual interface for browsing run results, screenshots,
and findings.

NOTE: The full dashboard server is implemented separately.
This command provides the CLI entry point and launch logic.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


def _find_evidence_dir() -> Path:
    """Locate the .ghostqa/evidence/ directory by searching upward from cwd."""
    current = Path.cwd()
    for base in [current, *current.parents]:
        candidate = base / ".ghostqa" / "evidence"
        if candidate.is_dir():
            return candidate
    return current / ".ghostqa" / "evidence"


def dashboard(
    port: int = typer.Option(
        8089,
        "--port",
        "-p",
        help="Port to serve the dashboard on.",
    ),
    evidence_dir: Optional[Path] = typer.Option(
        None,
        "--evidence-dir",
        "-e",
        help="Path to evidence directory. Default: .ghostqa/evidence/",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Don't automatically open the browser.",
    ),
) -> None:
    """Launch the GhostQA evidence dashboard viewer.

    Starts a local web server to browse run results, screenshots,
    findings, and cost breakdowns in a visual interface.
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

    url = f"http://localhost:{port}"

    console.print()
    console.print(
        Panel(
            f"[bold cyan]GhostQA Dashboard[/bold cyan]\n\n"
            f"  Serving at:     [link={url}]{url}[/link]\n"
            f"  Evidence dir:   {edir}\n\n"
            "[dim]Press Ctrl+C to stop.[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    if not no_open:
        webbrowser.open(url)

    # Launch the dashboard server
    # The full implementation will be a separate module (ghostqa.dashboard.server).
    # For now, use a minimal built-in HTTP server that serves evidence files.
    try:
        _serve_evidence(edir, port)
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard stopped.[/dim]")
    except OSError as exc:
        console.print(
            Panel(
                f"[red]Failed to start server:[/red] {exc}\n\n"
                f"Port {port} may be in use. Try [bold]--port {port + 1}[/bold].",
                title="[red]Server Error[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=3)


def _serve_evidence(evidence_dir: Path, port: int) -> None:
    """Serve evidence directory using Python's built-in HTTP server.

    This is a minimal fallback. The full dashboard with React/Jinja
    templates will be implemented in ghostqa.dashboard.server.
    """
    import functools
    import http.server

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(evidence_dir),
    )

    with http.server.HTTPServer(("", port), handler) as httpd:
        httpd.serve_forever()
