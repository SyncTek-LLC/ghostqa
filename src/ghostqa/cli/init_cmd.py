"""ghostqa init — Initialize a .ghostqa/ project directory.

Creates the directory structure, config template, and sample YAML files
that GhostQA needs to discover products, personas, and journeys.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

console = Console()

# ── Sample file contents (inline so `ghostqa init` works without examples/) ──

_SAMPLE_CONFIG = """\
# GhostQA project configuration
# Docs: https://github.com/SyncTek-LLC/ghostqa

# Default budget per run (USD)
budget: 5.00

# Default headless mode
headless: true

# Default viewport
viewport:
  width: 1280
  height: 720

# Timeout per run (seconds)
timeout: 600

# Uncomment to set your API key here (env var ANTHROPIC_API_KEY takes priority)
# anthropic_api_key: sk-ant-...
"""

_SAMPLE_PRODUCT = """\
product:
  name: myapp
  display_name: "My Application"
  base_url: "http://localhost:3000"

  services:
    frontend:
      url: "http://localhost:3000"
      health_endpoint: /

  viewports:
    desktop:
      width: 1280
      height: 720
    mobile:
      width: 375
      height: 812

  cost_limits:
    per_run_usd: 5.00
"""

_SAMPLE_PERSONA = """\
persona:
  name: alex_tester
  display_name: "Alex the Tester"
  role: "QA Engineer"
  age: 30
  tech_comfort: high
  patience: medium
  preferred_device: desktop

  goals:
    - "Verify core user flows work correctly"
    - "Find UX issues a real user would encounter"

  frustrations:
    - "Broken forms"
    - "Unclear error messages"
    - "Missing loading states"

  credentials:
    email: "alex@example.com"
    password: "TestPass123!"
"""

_SAMPLE_JOURNEY = """\
scenario:
  id: onboarding-happy-path
  name: "Onboarding Happy Path"
  description: "New user signs up, completes onboarding, reaches dashboard."
  tags: [onboarding, critical_path, smoke]

  personas:
    - ref: alex_tester
      role: primary

  preconditions:
    - service: frontend
      check: /
      expected_status: 200

  steps:
    - id: visit_homepage
      mode: browser
      description: "Navigate to the homepage and verify it loads"
      goal: "Navigate to the homepage and confirm it renders correctly"
      checkpoints:
        - type: text_present
          value: "Welcome"

    - id: navigate_signup
      mode: browser
      description: "Find and click the signup link"
      goal: "Locate the signup or register button and click it"
      checkpoints:
        - type: text_present
          value: "Sign"

    - id: fill_signup_form
      mode: browser
      description: "Fill out the registration form"
      goal: "Complete the signup form with test credentials"
      checkpoints:
        - type: text_present
          value: "email"

    - id: verify_dashboard
      mode: browser
      description: "Confirm signup succeeded and dashboard loads"
      goal: "Verify the user is logged in and can see the dashboard"
      checkpoints:
        - type: text_present
          value: "dashboard"
"""


def _copy_examples_or_inline(dest_dir: Path, subdir: str, filename: str, fallback: str) -> Path:
    """Try to copy from examples/ directory; fall back to inline content."""
    # Look for the examples directory relative to the ghostqa package
    pkg_root = Path(__file__).resolve().parent.parent.parent.parent  # src/ghostqa/cli -> repo root
    examples_src = pkg_root / "examples" / subdir / filename

    dest = dest_dir / filename
    if examples_src.is_file():
        shutil.copy2(examples_src, dest)
    else:
        dest.write_text(fallback, encoding="utf-8")
    return dest


def init(
    dir: Path = typer.Option(
        Path("."),
        "--dir",
        "-d",
        help="Parent directory for .ghostqa/ project. Defaults to current directory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing .ghostqa/ directory.",
    ),
) -> None:
    """Initialize a new GhostQA project directory.

    Creates .ghostqa/ with products/, personas/, journeys/, evidence/
    subdirectories, a config.yaml template, and sample YAML files.
    """
    project_dir = dir.resolve() / ".ghostqa"

    if project_dir.exists() and not force:
        console.print(
            Panel(
                f"[yellow]Directory already exists:[/yellow] {project_dir}\n\n"
                "Use [bold]--force[/bold] to overwrite.",
                title="Already Initialized",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)

    # Create directory tree
    subdirs = ["products", "personas", "journeys", "evidence"]
    for sub in subdirs:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    # Write config template
    config_path = project_dir / "config.yaml"
    config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

    # Write sample files (try examples/ first, fall back to inline)
    _copy_examples_or_inline(project_dir / "products", "products", "demo.yaml", _SAMPLE_PRODUCT)
    _copy_examples_or_inline(project_dir / "personas", "personas", "alex-developer.yaml", _SAMPLE_PERSONA)
    _copy_examples_or_inline(project_dir / "journeys", "journeys", "demo-onboarding.yaml", _SAMPLE_JOURNEY)

    # Display result as a Rich tree
    tree = Tree(f"[bold green]{project_dir}[/bold green]", guide_style="dim")
    tree.add("[cyan]config.yaml[/cyan]")

    for sub in subdirs:
        branch = tree.add(f"[blue]{sub}/[/blue]")
        for child in sorted((project_dir / sub).iterdir()):
            if child.is_file():
                branch.add(f"[dim]{child.name}[/dim]")

    console.print()
    console.print(
        Panel(
            tree,
            title="[bold green]GhostQA Initialized[/bold green]",
            border_style="green",
        )
    )
    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print("  1. Edit [cyan].ghostqa/products/demo.yaml[/cyan] with your app's URL")
    console.print("  2. Customize personas and journeys")
    console.print("  3. Run [bold]ghostqa install[/bold] to set up Playwright")
    console.print("  4. Run [bold]ghostqa run --product demo[/bold]")
    console.print()
