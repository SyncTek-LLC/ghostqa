"""specterqa init — Initialize a .specterqa/ project directory.

Creates the directory structure, config template, and sample YAML files
that SpecterQA needs to discover products, personas, and journeys.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

console = Console()

# ── Sample file contents (inline so `specterqa init` works without examples/) ──

_SAMPLE_CONFIG = """\
# SpecterQA project configuration
# Docs: https://github.com/SyncTek-LLC/specterqa

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

  # SECURITY (FIND-003): Use environment variable references for credentials.
  # Never store real credentials as literal values in persona YAML files.
  # Set TEST_EMAIL and TEST_PASSWORD in your environment or .env file before running.
  credentials:
    email: "${TEST_EMAIL}"
    password: "${TEST_PASSWORD}"
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
    # Look for the examples directory relative to the specterqa package
    pkg_root = Path(__file__).resolve().parent.parent.parent.parent  # src/specterqa/cli -> repo root
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
        help="Parent directory for .specterqa/ project. Defaults to current directory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing .specterqa/ directory.",
    ),
) -> None:
    """Initialize a new SpecterQA project directory.

    Creates .specterqa/ with products/, personas/, journeys/, evidence/
    subdirectories, a config.yaml template, and sample YAML files.
    """
    project_dir = dir.resolve() / ".specterqa"

    if project_dir.exists() and not force:
        console.print(
            Panel(
                f"[yellow]Directory already exists:[/yellow] {project_dir}\n\nUse [bold]--force[/bold] to overwrite.",
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

    # SECURITY (FIND-003): Ensure .gitignore in the project root includes
    # .specterqa/personas/ so persona YAML files (which may contain credential
    # references) are not accidentally committed to version control.
    parent_dir = project_dir.parent
    gitignore_path = parent_dir / ".gitignore"
    _personas_gitignore_entry = ".specterqa/personas/"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        if _personas_gitignore_entry not in existing:
            gitignore_path.write_text(
                existing.rstrip("\n")
                + f"\n\n# SpecterQA — persona files may contain credential references\n{_personas_gitignore_entry}\n",
                encoding="utf-8",
            )
    else:
        gitignore_path.write_text(
            f"# SpecterQA — persona files may contain credential references\n{_personas_gitignore_entry}\n",
            encoding="utf-8",
        )

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
            title="[bold green]SpecterQA Initialized[/bold green]",
            border_style="green",
        )
    )
    import os

    api_key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))

    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print("  1. Edit [cyan].specterqa/products/demo.yaml[/cyan] with your app's URL")
    console.print("  2. Customize personas and journeys")
    console.print("  3. Run [bold]specterqa install[/bold] to set up Playwright")

    if not api_key_set:
        console.print()
        console.print(
            Panel(
                "[bold yellow]Set your API key before running tests:[/bold yellow]\n\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n\n"
                "Get a key at: https://console.anthropic.com/\n\n"
                "You can also store it in [cyan].specterqa/config.yaml[/cyan]:\n"
                "  [dim]anthropic_api_key: sk-ant-...[/dim]",
                title="[yellow]API Key Required[/yellow]",
                border_style="yellow",
            )
        )
    else:
        console.print("  4. [green]ANTHROPIC_API_KEY already set \u2713[/green]")

    console.print()
    console.print("  Run: [bold]specterqa run --product demo[/bold]")
    console.print()
