"""GhostQA MCP Server — Model Context Protocol server for AI agent integration.

Exposes GhostQA's behavioral testing engine as MCP tools so that AI agents
can discover products, run tests, and retrieve results programmatically.

Usage:
    ghostqa-mcp            # stdio transport (default)
    python -m ghostqa.mcp  # alternative invocation

The server provides four tools:

    ghostqa_run            Run behavioral tests against a product
    ghostqa_list_products  List available products and their journeys
    ghostqa_get_results    Retrieve results from a previous run
    ghostqa_init           Initialize a new GhostQA project directory
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("ghostqa.mcp")

# SECURITY (FIND-002): Optional allowlist of base directories the MCP server
# is permitted to access.  Controlled via the GHOSTQA_ALLOWED_DIRS environment
# variable — a colon-separated list of absolute paths.
#
# When GHOSTQA_ALLOWED_DIRS is set, any `directory` argument that does NOT
# resolve to within one of the listed bases is rejected.  This prevents an
# AI agent from pointing GhostQA at arbitrary filesystem locations (e.g.
# "/etc", "/home/other-user").
#
# When GHOSTQA_ALLOWED_DIRS is NOT set (the default), all directories are
# permitted.  Operators running the MCP server in shared or multi-user
# environments are strongly encouraged to set this variable.
def _build_allowed_dirs() -> list[Path] | None:
    """Build the list of allowed base directories from the environment.

    Returns None if GHOSTQA_ALLOWED_DIRS is not set (no restriction active).
    """
    env_val = os.environ.get("GHOSTQA_ALLOWED_DIRS", "")
    if env_val.strip():
        return [Path(p).resolve() for p in env_val.split(":") if p.strip()]
    return None  # No restriction configured


# Computed once at import time so the allowlist is stable across all tool calls.
_ALLOWED_DIRS: list[Path] | None = _build_allowed_dirs()


def _validate_directory(directory: str | None) -> tuple[Path | None, str | None]:
    """Resolve *directory* and verify it is within an allowed base (if configured).

    Returns (resolved_path, None) on success, or (None, error_message) if
    the path is outside all allowed bases when GHOSTQA_ALLOWED_DIRS is set.

    SECURITY (FIND-002): Prevents path traversal by an MCP-connected AI agent
    when the operator has configured directory restrictions.
    """
    start = Path(directory).resolve() if directory else Path.cwd().resolve()
    logger.info("MCP directory request: %s (resolved: %s)", directory, start)

    # No restriction configured — allow all directories
    if _ALLOWED_DIRS is None:
        return start, None

    for allowed in _ALLOWED_DIRS:
        try:
            start.relative_to(allowed)
            return start, None  # Within an allowed base — OK
        except ValueError:
            continue

    allowed_list = ", ".join(str(p) for p in _ALLOWED_DIRS)
    return None, (
        f"Directory access denied: {start}\n\n"
        f"The MCP server only allows access within: {allowed_list}\n\n"
        "To allow additional directories, set the GHOSTQA_ALLOWED_DIRS "
        "environment variable to a colon-separated list of permitted base paths."
    )


def _resolve_project_dir(directory: str | None = None) -> Path:
    """Find the .ghostqa/ project directory.

    Searches upward from *directory* (default: cwd) for a .ghostqa/ folder.
    Returns the path to the .ghostqa directory, or a default if none found.

    Note: Callers must have already validated *directory* via _validate_directory()
    before this function is called.
    """
    start = Path(directory).resolve() if directory else Path.cwd()

    # Check the directory itself
    candidate = start / ".ghostqa"
    if candidate.is_dir():
        return candidate

    # Walk up parents
    for parent in start.parents:
        candidate = parent / ".ghostqa"
        if candidate.is_dir():
            return candidate

    # Fallback: use start/.ghostqa (may not exist yet)
    return start / ".ghostqa"


def _check_project_initialized(project_dir: Path) -> str | None:
    """Return an error message if the project is not initialized, else None."""
    if not project_dir.is_dir():
        return (
            f"GhostQA project not initialized at {project_dir.parent}\n\n"
            "Run ghostqa_init first, or use the ghostqa CLI:\n"
            "  ghostqa init --dir /path/to/your/project"
        )
    return None


def _check_playwright_available() -> str | None:
    """Return an error message if Playwright is not importable, else None."""
    try:
        import playwright  # noqa: F401

        return None
    except ImportError:
        return (
            "Playwright is not installed. GhostQA requires Playwright for browser-based testing.\n\n"
            "Install it:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )


def _build_config(
    project_dir: Path,
    budget: float | None = None,
    headless: bool = True,
    level: str = "standard",
) -> Any:
    """Build a GhostQAConfig from the project directory.

    Returns:
        A GhostQAConfig instance.

    Raises:
        Exception if config loading fails.
    """
    from ghostqa.config import GhostQAConfig

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

    config.level = level
    config.headless = headless
    if budget is not None:
        config.budget = budget

    # Resolve API key (best-effort; orchestrator will fail with clear error if missing)
    try:
        from ghostqa.credentials import resolve_api_key

        config.anthropic_api_key = resolve_api_key(project_dir)
    except Exception:
        pass  # Let orchestrator report the missing key

    return config


def _load_run_result(evidence_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Load a run-result.json for a specific run_id.

    Returns the parsed dict or None if not found.
    """
    run_dir = evidence_dir / run_id
    result_file = run_dir / "run-result.json"
    if result_file.is_file():
        try:
            return json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _list_all_run_ids(evidence_dir: Path) -> list[str]:
    """List all run IDs in the evidence directory, most recent first."""
    if not evidence_dir.is_dir():
        return []
    return sorted(
        [d.name for d in evidence_dir.iterdir() if d.is_dir() and d.name.startswith("GQA-RUN-")],
        reverse=True,
    )


def _json_serialize(obj: Any) -> str:
    """JSON serializer for non-standard types."""
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def create_server() -> Any:
    """Create and configure the GhostQA MCP server.

    Returns:
        A FastMCP server instance with all tools registered.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise ImportError(
            "The 'mcp' package is required for the GhostQA MCP server.\n\n"
            "Install it:\n"
            "  pip install 'ghostqa[mcp]'\n"
            "  # or: pip install mcp>=1.0.0"
        )

    mcp = FastMCP(
        "ghostqa",
        instructions=(
            "GhostQA is an AI-powered behavioral testing tool. "
            "It uses AI personas to navigate and test web applications "
            "through real browser sessions, evaluating UX, functionality, "
            "and error handling via vision-based interaction. "
            "Configure products, personas, and journeys as YAML, then run "
            "tests to get structured pass/fail results with findings."
        ),
    )

    # ── Tool: ghostqa_run ────────────────────────────────────────────────

    @mcp.tool(
        name="ghostqa_run",
        description=(
            "Run GhostQA behavioral tests against a product. "
            "AI personas navigate your app in real browser sessions and report "
            "UX issues, broken flows, and errors. Returns structured results "
            "with pass/fail per step, findings, and cost tracking."
        ),
    )
    async def ghostqa_run(
        product: str,
        journey: str | None = None,
        level: str = "standard",
        budget: float = 5.0,
        headless: bool = True,
        directory: str | None = None,
    ) -> str:
        """Run behavioral tests against a product.

        Args:
            product: Product name (must match a YAML file in .ghostqa/products/).
            journey: Specific journey/scenario ID to run. Omit to run all journeys.
            level: Test level -- "smoke" (quick, 1 scenario), "standard" (all),
                   or "thorough" (all + extra coverage).
            budget: Maximum spend for this run in USD. Default: $5.00.
            headless: Run browser in headless mode. Default: true.
            directory: Working directory to search for .ghostqa/ project.
                       Defaults to the server's working directory.

        Returns:
            JSON string with run results including pass/fail, findings, and cost.
        """
        # Validate level
        valid_levels = {"smoke", "standard", "thorough"}
        if level not in valid_levels:
            return json.dumps({
                "error": f"Invalid level: {level}. Valid levels: {', '.join(sorted(valid_levels))}",
                "tool_error": True,
                "error_code": "CONFIG_ERROR",
            })

        # SECURITY (FIND-002): Validate directory is within allowed bases
        _, dir_err = _validate_directory(directory)
        if dir_err:
            return json.dumps({"error": dir_err, "tool_error": True, "error_code": "CONFIG_ERROR"})

        # Find project
        project_dir = _resolve_project_dir(directory)
        init_err = _check_project_initialized(project_dir)
        if init_err:
            return json.dumps({"error": init_err, "tool_error": True, "error_code": "PROJECT_NOT_INITIALIZED"})

        # Check playwright
        pw_err = _check_playwright_available()
        if pw_err:
            return json.dumps({"error": pw_err, "tool_error": True, "error_code": "PLAYWRIGHT_NOT_INSTALLED"})

        # Build config
        try:
            config = _build_config(
                project_dir=project_dir,
                budget=budget,
                headless=headless,
                level=level,
            )
        except Exception as exc:
            return json.dumps({"error": f"Configuration error: {exc}", "tool_error": True, "error_code": "CONFIG_ERROR"})

        # Run the orchestrator (synchronous -- runs in thread)
        try:
            from ghostqa.engine.orchestrator import GhostQAOrchestrator

            orchestrator = GhostQAOrchestrator(config)
            _report_md, all_passed = orchestrator.run(
                product=product,
                scenario_id=journey,
                level=level,
            )
        except Exception as exc:
            logger.exception("Run failed")
            return json.dumps({
                "error": f"Run failed: {exc}",
                "tool_error": True,
                "error_code": "INTERNAL_ERROR",
            })

        # Load structured result from evidence directory
        evidence_dir = config.evidence_dir
        run_ids = _list_all_run_ids(evidence_dir)
        if not run_ids:
            return json.dumps({
                "passed": all_passed,
                "run_id": "unknown",
                "summary": {"total_steps": 0, "passed": 0, "failed": 0, "findings_count": 0},
                "findings": [],
                "cost_usd": 0.0,
                "evidence_dir": str(evidence_dir),
            })

        run_id = run_ids[0]  # Most recent
        result_data = _load_run_result(evidence_dir, run_id)

        if result_data:
            step_reports = result_data.get("step_reports", [])
            findings = result_data.get("findings", [])
            passed_count = sum(1 for s in step_reports if s.get("passed"))
            return json.dumps({
                "passed": result_data.get("passed", all_passed),
                "run_id": result_data.get("run_id", run_id),
                "summary": {
                    "total_steps": len(step_reports),
                    "passed": passed_count,
                    "failed": len(step_reports) - passed_count,
                    "findings_count": len(findings),
                },
                "findings": [
                    {
                        "severity": f.get("severity", "unknown"),
                        "category": f.get("category", "unknown"),
                        "description": f.get("description", ""),
                        "step_id": f.get("step_id", ""),
                    }
                    for f in findings
                ],
                "cost_usd": result_data.get("cost_usd", 0.0),
                "evidence_dir": str(evidence_dir / run_id),
            }, default=_json_serialize)
        else:
            return json.dumps({
                "passed": all_passed,
                "run_id": run_id,
                "summary": {"total_steps": 0, "passed": 0, "failed": 0, "findings_count": 0},
                "findings": [],
                "cost_usd": 0.0,
                "evidence_dir": str(evidence_dir / run_id),
            })

    # ── Tool: ghostqa_list_products ──────────────────────────────────────

    @mcp.tool(
        name="ghostqa_list_products",
        description=(
            "List all available GhostQA products and their journeys. "
            "Shows product names, base URLs, app types, and which journeys "
            "are configured for testing."
        ),
    )
    async def ghostqa_list_products(
        directory: str | None = None,
    ) -> str:
        """List available products and their journeys.

        Args:
            directory: Working directory to search for .ghostqa/ project.
                       Defaults to the server's working directory.

        Returns:
            JSON array of products with their journeys.
        """
        # SECURITY (FIND-002): Validate directory is within allowed bases
        _, dir_err = _validate_directory(directory)
        if dir_err:
            return json.dumps({"error": dir_err, "tool_error": True, "error_code": "CONFIG_ERROR"})

        project_dir = _resolve_project_dir(directory)
        init_err = _check_project_initialized(project_dir)
        if init_err:
            return json.dumps({"error": init_err, "tool_error": True, "error_code": "PROJECT_NOT_INITIALIZED"})

        products_dir = project_dir / "products"
        if not products_dir.is_dir():
            return json.dumps({
                "error": f"Products directory not found: {products_dir}",
                "tool_error": True,
                "error_code": "PROJECT_NOT_INITIALIZED",
            })

        try:
            import yaml  # type: ignore
        except ImportError:
            return json.dumps({
                "error": "PyYAML not installed. Install it: pip install pyyaml",
                "tool_error": True,
                "error_code": "CONFIG_ERROR",
            })

        results: list[dict[str, Any]] = []

        for product_file in sorted(products_dir.glob("*.yaml")):
            try:
                with open(product_file, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", product_file, exc)
                continue

            product = data.get("product", data)
            product_name = product.get("name", product_file.stem)
            base_url = ""
            services = product.get("services", {})
            if services:
                frontend = services.get("frontend", {})
                base_url = frontend.get("url", "")
            if not base_url:
                base_url = product.get("base_url", "")

            app_type = product.get("app_type", "web")

            # Find journeys for this product
            journeys = _find_journeys_for_product(project_dir, product_name)

            results.append({
                "name": product_name,
                "base_url": base_url,
                "app_type": app_type,
                "journeys": journeys,
            })

        # Also check for directory-style products (product/_product.yaml)
        for subdir in sorted(products_dir.iterdir()):
            if not subdir.is_dir():
                continue
            product_file = subdir / "_product.yaml"
            if not product_file.is_file():
                continue

            # Skip if we already found a flat-file version
            if any(p["name"] == subdir.name for p in results):
                continue

            try:
                with open(product_file, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", product_file, exc)
                continue

            product = data.get("product", data)
            product_name = product.get("name", subdir.name)
            base_url = ""
            services = product.get("services", {})
            if services:
                frontend = services.get("frontend", {})
                base_url = frontend.get("url", "")
            if not base_url:
                base_url = product.get("base_url", "")

            app_type = product.get("app_type", "web")
            journeys = _find_journeys_for_product(project_dir, product_name)

            results.append({
                "name": product_name,
                "base_url": base_url,
                "app_type": app_type,
                "journeys": journeys,
            })

        return json.dumps(results, indent=2)

    # ── Tool: ghostqa_get_results ────────────────────────────────────────

    @mcp.tool(
        name="ghostqa_get_results",
        description=(
            "Get full results from a previous GhostQA run by run ID. "
            "Returns the complete structured result including step reports, "
            "findings, cost breakdown, and evidence paths."
        ),
    )
    async def ghostqa_get_results(
        run_id: str,
        directory: str | None = None,
    ) -> str:
        """Get results from a previous GhostQA run.

        Args:
            run_id: The run ID (e.g., "GQA-RUN-20260222-143052-a1b2").
            directory: Working directory to search for .ghostqa/ project.
                       Defaults to the server's working directory.

        Returns:
            JSON string with the full run result, or an error.
        """
        # SECURITY (FIND-002): Validate directory is within allowed bases
        _, dir_err = _validate_directory(directory)
        if dir_err:
            return json.dumps({"error": dir_err, "tool_error": True, "error_code": "CONFIG_ERROR"})

        project_dir = _resolve_project_dir(directory)
        init_err = _check_project_initialized(project_dir)
        if init_err:
            return json.dumps({"error": init_err, "tool_error": True, "error_code": "PROJECT_NOT_INITIALIZED"})

        evidence_dir = project_dir / "evidence"
        if not evidence_dir.is_dir():
            return json.dumps({
                "error": f"Evidence directory not found: {evidence_dir}",
                "tool_error": True,
                "error_code": "PROJECT_NOT_INITIALIZED",
            })

        # Try to load the specific run
        result_data = _load_run_result(evidence_dir, run_id)
        if result_data:
            return json.dumps(result_data, indent=2, default=_json_serialize)

        # If not found, list available runs to help the user
        available = _list_all_run_ids(evidence_dir)
        if available:
            return json.dumps({
                "error": f"Run ID not found: {run_id}",
                "tool_error": True,
                "error_code": "RUN_NOT_FOUND",
                "available_run_ids": available[:20],  # Show up to 20 most recent
            })
        else:
            return json.dumps({
                "error": f"Run ID not found: {run_id}",
                "tool_error": True,
                "error_code": "RUN_NOT_FOUND",
                "available_run_ids": [],
                "hint": "No runs have been recorded yet. Use ghostqa_run to execute tests first.",
            })

    # ── Tool: ghostqa_init ───────────────────────────────────────────────

    @mcp.tool(
        name="ghostqa_init",
        description=(
            "Initialize a new GhostQA project directory. "
            "Creates the .ghostqa/ directory with config, sample products, "
            "personas, and journeys. Use this before running any tests."
        ),
    )
    async def ghostqa_init(
        directory: str = ".",
        url: str | None = None,
    ) -> str:
        """Initialize a GhostQA project.

        Args:
            directory: Parent directory for .ghostqa/ project. Default: current directory.
            url: Optional base URL for the product. If provided, the sample product
                 config will be updated with this URL.

        Returns:
            JSON with success status and list of created files.
        """
        # SECURITY (FIND-002): Validate directory is within allowed bases
        _, dir_err = _validate_directory(directory)
        if dir_err:
            return json.dumps({"success": False, "error": dir_err})

        target = Path(directory).resolve()
        project_dir = target / ".ghostqa"

        if project_dir.exists():
            return json.dumps({
                "success": False,
                "error": f"GhostQA project already initialized at {project_dir}",
                "hint": "The .ghostqa/ directory already exists. Delete it first to re-initialize.",
            })

        # Create directory structure
        subdirs = ["products", "personas", "journeys", "evidence"]
        created_files: list[str] = []

        try:
            for sub in subdirs:
                (project_dir / sub).mkdir(parents=True, exist_ok=True)
                created_files.append(f".ghostqa/{sub}/")

            # Import inline templates from the init_cmd module
            from ghostqa.cli.init_cmd import (
                _SAMPLE_CONFIG,
                _SAMPLE_JOURNEY,
                _SAMPLE_PERSONA,
                _SAMPLE_PRODUCT,
            )

            # Write config
            config_path = project_dir / "config.yaml"
            config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
            created_files.append(".ghostqa/config.yaml")

            # Write sample product
            product_content = _SAMPLE_PRODUCT
            if url:
                # Patch the sample product with the provided URL
                product_content = product_content.replace(
                    "http://localhost:3000", url
                )
            product_path = project_dir / "products" / "demo.yaml"
            product_path.write_text(product_content, encoding="utf-8")
            created_files.append(".ghostqa/products/demo.yaml")

            # Write sample persona
            persona_path = project_dir / "personas" / "alex-developer.yaml"
            persona_path.write_text(_SAMPLE_PERSONA, encoding="utf-8")
            created_files.append(".ghostqa/personas/alex-developer.yaml")

            # Write sample journey
            journey_path = project_dir / "journeys" / "demo-onboarding.yaml"
            journey_path.write_text(_SAMPLE_JOURNEY, encoding="utf-8")
            created_files.append(".ghostqa/journeys/demo-onboarding.yaml")

            # SECURITY (FIND-003): Ensure .gitignore in the parent directory
            # includes .ghostqa/personas/ so persona files (which may contain
            # credential environment variable references) are not committed.
            _personas_entry = ".ghostqa/personas/"
            gitignore_path = target / ".gitignore"
            if gitignore_path.exists():
                existing = gitignore_path.read_text(encoding="utf-8")
                if _personas_entry not in existing:
                    gitignore_path.write_text(
                        existing.rstrip("\n")
                        + f"\n\n# GhostQA — persona files may contain credential references\n{_personas_entry}\n",
                        encoding="utf-8",
                    )
            else:
                gitignore_path.write_text(
                    f"# GhostQA — persona files may contain credential references\n{_personas_entry}\n",
                    encoding="utf-8",
                )
            created_files.append(".gitignore (updated with .ghostqa/personas/)")

        except Exception as exc:
            return json.dumps({
                "success": False,
                "error": f"Failed to create project: {exc}",
            })

        import os

        api_key_configured = bool(os.environ.get("ANTHROPIC_API_KEY"))

        return json.dumps({
            "success": True,
            "project_dir": str(project_dir),
            "files_created": created_files,
            "api_key_configured": api_key_configured,
            "next_steps": [
                f"Edit .ghostqa/products/demo.yaml with your app's URL"
                + (f" (set to {url})" if url else ""),
                "Customize personas and journeys for your app",
                "Install Playwright: playwright install chromium",
                *([] if api_key_configured else ["Set ANTHROPIC_API_KEY: export ANTHROPIC_API_KEY=sk-ant-..."]),
                "Run tests: use the ghostqa_run tool with product='demo'",
            ],
        })

    # ── Tool: ghostqa_budget_check ───────────────────────────────────────

    @mcp.tool(
        name="ghostqa_budget_check",
        description=(
            "Check cumulative GhostQA cost against daily and monthly budget limits. "
            "Call this before ghostqa_run to avoid hard budget-exceeded errors mid-run. "
            "Returns current spend, limits, and whether a run can proceed. Zero cost — no API calls made."
        ),
    )
    async def ghostqa_budget_check(
        directory: str | None = None,
        per_day_usd: float = 0.0,
        per_month_usd: float = 0.0,
    ) -> str:
        """Check cumulative budget status without running tests.

        Args:
            directory: Working directory to search for .ghostqa/ project.
                       Defaults to the server's working directory.
            per_day_usd: Daily spend limit in USD. 0.0 means no limit.
            per_month_usd: Monthly spend limit in USD. 0.0 means no limit.

        Returns:
            JSON string with current budget status including daily/monthly spend,
            limits, and whether a run can proceed.
        """
        project_dir = _resolve_project_dir(directory)
        init_err = _check_project_initialized(project_dir)
        if init_err:
            return json.dumps({
                "error": init_err,
                "tool_error": True,
                "error_code": "PROJECT_NOT_INITIALIZED",
            })

        try:
            from ghostqa.engine.cost_tracker import CostTracker

            # Use evidence dir as the base for the cost ledger
            base_dir = project_dir / "evidence"
            budget_status = CostTracker.check_cumulative_budget(
                base_dir=base_dir,
                per_day_usd=per_day_usd,
                per_month_usd=per_month_usd,
            )
        except Exception as exc:
            return json.dumps({
                "error": f"Budget check failed: {exc}",
                "tool_error": True,
                "error_code": "INTERNAL_ERROR",
            })

        daily_ok = budget_status["daily_ok"]
        monthly_ok = budget_status["monthly_ok"]
        can_run = daily_ok and monthly_ok

        reason: str | None = None
        if not daily_ok:
            reason = (
                f"Daily budget limit reached: "
                f"${budget_status['daily_spent']:.4f} spent of ${per_day_usd:.2f} daily limit"
            )
        elif not monthly_ok:
            reason = (
                f"Monthly budget limit reached: "
                f"${budget_status['monthly_spent']:.4f} spent of ${per_month_usd:.2f} monthly limit"
            )

        return json.dumps({
            "can_run": can_run,
            "daily_ok": daily_ok,
            "monthly_ok": monthly_ok,
            "daily_spent_usd": budget_status["daily_spent"],
            "daily_limit_usd": per_day_usd,
            "monthly_spent_usd": budget_status["monthly_spent"],
            "monthly_limit_usd": per_month_usd,
            "reason": reason,
        })

    return mcp


def _find_journeys_for_product(project_dir: Path, product_name: str) -> list[dict[str, str]]:
    """Find journeys associated with a product.

    Looks in both the global journeys/ dir and product-scoped journeys.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return []

    journeys: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    # Check global journeys directory
    journeys_dir = project_dir / "journeys"
    if journeys_dir.is_dir():
        for f in sorted(journeys_dir.glob("*.yaml")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                scenario = data.get("scenario", data)
                sid = scenario.get("id", f.stem)
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    journeys.append({
                        "id": sid,
                        "name": scenario.get("name", sid),
                        "tags": scenario.get("tags", []),
                    })
            except Exception:
                continue

    # Check product-scoped journeys (inside .ghostqa/<product>/journeys/)
    product_journeys_dir = project_dir / product_name / "journeys"
    if product_journeys_dir.is_dir():
        for f in sorted(product_journeys_dir.glob("*.yaml")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                scenario = data.get("scenario", data)
                sid = scenario.get("id", f.stem)
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    journeys.append({
                        "id": sid,
                        "name": scenario.get("name", sid),
                        "tags": scenario.get("tags", []),
                    })
            except Exception:
                continue

    return journeys


def main() -> None:
    """Entry point for the ghostqa-mcp command."""
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
