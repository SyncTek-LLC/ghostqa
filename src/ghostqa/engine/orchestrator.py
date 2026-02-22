"""GhostQA Orchestrator — The main coordinator for test runs.

Reads scenario/persona/product YAML, checks preconditions, resolves template
variables, routes steps to the appropriate runner (API or browser), collects
results, generates reports, and tracks costs.

Decoupled from BusinessAtlas VTE — all paths are config-driven via
GhostQAConfig.  No hardcoded repository paths.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import random
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

from ghostqa.config import GhostQAConfig
from ghostqa.engine.cost_tracker import BudgetExceededError, CostTracker
from ghostqa.engine.report_generator import (
    Finding,
    ReportGenerator,
    StepReport,
    VTERunResult,
)

logger = logging.getLogger("ghostqa.engine.orchestrator")


class GhostQAOrchestrator:
    """Coordinates a complete GhostQA run: load config, check preconditions,
    execute steps, generate report.
    """

    _MISSING = object()  # Sentinel for "dotpath not found"

    # Flag set by signal handler for graceful shutdown
    _shutdown_requested: bool = False

    def __init__(self, config: GhostQAConfig) -> None:
        """
        Args:
            config: GhostQAConfig instance with all paths and settings.
        """
        self._config = config

        # Derive the scenarios root from the config.
        # products_dir is typically <project>/.ghostqa/products; the
        # scenarios root is its parent (i.e. <project>/.ghostqa/).
        self._scenarios_root = config.products_dir.parent

        self._report_generator = ReportGenerator()

    # ── Process Management ──────────────────────────────────────────────

    @classmethod
    def _install_signal_handlers(cls) -> None:
        """Install signal handlers for graceful shutdown.

        Sets a shutdown flag instead of writing PID files to any
        specific location.
        """

        def _handle_term(signum: int, frame: Any) -> None:
            logger.info("Received signal %d — requesting graceful shutdown", signum)
            cls._shutdown_requested = True
            raise SystemExit(1)

        signal.signal(signal.SIGTERM, _handle_term)

    @staticmethod
    def _write_pid_file(evidence_dir: Path) -> Path:
        """Write a PID file for the current process.

        Returns the path to the PID file.
        """
        pid_file = evidence_dir / "run.pid"
        pid_file.write_text(str(os.getpid()))
        return pid_file

    @staticmethod
    def _write_status_file(
        evidence_dir: Path,
        status: str,
        run_id: str,
        scenario: str,
        product: str,
        start_time: str,
        end_time: str | None = None,
        passed: bool | None = None,
    ) -> None:
        """Write or update the run-status.json file."""
        data: dict[str, Any] = {
            "status": status,
            "pid": os.getpid(),
            "start_time": start_time,
            "scenario": scenario,
            "product": product,
            "run_id": run_id,
        }
        if end_time is not None:
            data["end_time"] = end_time
        if passed is not None:
            data["passed"] = passed
        try:
            (evidence_dir / "run-status.json").write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to write run-status.json: %s", exc)

    @staticmethod
    def _cleanup_pid_file(evidence_dir: Path) -> None:
        """Remove the PID file if it exists."""
        pid_file = evidence_dir / "run.pid"
        try:
            pid_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to remove PID file: %s", exc)

    def run(
        self,
        product: str,
        scenario_id: str | None = None,
        level: str = "standard",
        viewport: str | None = None,
        tag: str | None = None,
    ) -> tuple[str, bool]:
        """Execute a GhostQA run.

        Args:
            product: Product slug (e.g., "myapp").
            scenario_id: Specific scenario ID to run (or None for all).
            level: Run level — "smoke", "standard", or "full".
            viewport: Override viewport for all browser steps.
            tag: Only run scenarios with this tag.

        Returns:
            Tuple of (report_markdown, all_passed).
        """
        # Generate unique run ID
        run_id = self._generate_run_id()
        logger.info("GhostQA run %s starting — product=%s, level=%s", run_id, product, level)

        # Install signal handlers for graceful shutdown
        self._install_signal_handlers()

        # Load product config
        product_config = self._load_product_config(product)
        if product_config is None:
            return f"# GhostQA Error\n\nProduct config not found: {product}", False

        # Load scenarios
        scenarios = self._load_scenarios(product, scenario_id, tag)
        if not scenarios:
            return f"# GhostQA Error\n\nNo scenarios found for product={product}", False

        # For smoke level, only run the first scenario
        if level == "smoke":
            scenarios = scenarios[:1]

        # Create evidence directory using config-driven path
        evidence_dir = self._config.evidence_dir / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)

        start_time_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        first_scenario_name = ""
        if scenarios:
            first_scn = scenarios[0].get("scenario", scenarios[0])
            first_scenario_name = first_scn.get("name", "Unknown")

        self._write_pid_file(evidence_dir)
        self._write_status_file(
            evidence_dir=evidence_dir,
            status="running",
            run_id=run_id,
            scenario=first_scenario_name,
            product=product_config.get("name", product),
            start_time=start_time_iso,
        )

        all_reports: list[str] = []
        all_passed = True

        try:
            for scenario_def in scenarios:
                scenario = scenario_def.get("scenario", scenario_def)

                # Check holdout
                if scenario.get("holdout", False):
                    logger.info("Skipping holdout scenario: %s", scenario.get("id", "?"))
                    continue

                report, passed = self._run_scenario(
                    run_id=run_id,
                    product=product,
                    product_config=product_config,
                    scenario=scenario,
                    level=level,
                    viewport_override=viewport,
                )
                all_reports.append(report)
                if not passed:
                    all_passed = False

            combined_report = "\n\n---\n\n".join(all_reports)
            return combined_report, all_passed

        except (SystemExit, KeyboardInterrupt):
            logger.info("GhostQA run %s terminated by signal", run_id)
            all_passed = False
            terminated_msg = "# GhostQA Run Terminated\n\nRun was terminated by external signal."
            combined_report = "\n\n---\n\n".join(all_reports) if all_reports else terminated_msg
            return combined_report, False

        finally:
            # CRITICAL: Write final status FIRST, then clean up PID file.
            # This order ensures that if the process is killed between these
            # two operations, status reflects the true outcome even if the
            # PID file lingers.  The dashboard treats "PID file exists but
            # process is dead" as stale, so a lingering PID file is harmless.
            # The reverse (PID gone but status still "running") is the
            # dangerous case — the dashboard would show "running" forever.
            final_status = "completed" if all_passed else "failed"
            end_time_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            try:
                self._write_status_file(
                    evidence_dir=evidence_dir,
                    status=final_status,
                    run_id=run_id,
                    scenario=first_scenario_name,
                    product=product_config.get("name", product),
                    start_time=start_time_iso,
                    end_time=end_time_iso,
                    passed=all_passed,
                )
            except Exception as exc:
                logger.error("CRITICAL: Failed to write final run-status.json: %s", exc)
            self._cleanup_pid_file(evidence_dir)

    def _run_scenario(
        self,
        run_id: str,
        product: str,
        product_config: dict[str, Any],
        scenario: dict[str, Any],
        level: str,
        viewport_override: str | None = None,
    ) -> tuple[str, bool]:
        """Run a single scenario and return (report, passed)."""
        scenario_name = scenario.get("name", "Unknown Scenario")
        scenario_id = scenario.get("id", "SCN-UNKNOWN")

        logger.info("Running scenario: %s (%s)", scenario_name, scenario_id)

        # Load personas
        persona_refs = scenario.get("personas", [])
        if not persona_refs:
            return f"# GhostQA Error\n\nNo personas defined in scenario {scenario_id}", False

        # For smoke level, only use the first (primary) persona
        if level == "smoke":
            persona_refs = [persona_refs[0]]

        # Load the primary persona
        primary_ref = persona_refs[0].get("ref", "")
        persona = self._load_persona(product, primary_ref)
        if persona is None:
            return f"# GhostQA Error\n\nPersona not found: {primary_ref}", False

        # Initialize cost tracker from product config
        cost_limits = product_config.get("cost_limits", {})
        cost_tracker = CostTracker(
            per_run_usd=cost_limits.get("per_run_usd", self._config.budget),
            warn_at_pct=cost_limits.get("warn_at_pct", 80),
            system_ledger_path=self._config.system_ledger_path,
            initiative_id=self._config.initiative_id,
        )

        # Evidence directory — config-driven
        evidence_dir = self._config.evidence_dir / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)

        # Compute start time early for run-meta.json
        start_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

        # Write run metadata early so consumers can show in-progress run info
        run_meta = {
            "run_id": run_id,
            "scenario_name": scenario_name,
            "scenario_id": scenario_id,
            "product_name": product_config.get("name", product),
            "persona_name": persona.get("name", "Unknown"),
            "persona_role": persona.get("demographics", {}).get("role", "Unknown"),
            "viewport_name": viewport_override or persona.get("preferred_device", "desktop"),
            "mock_level": scenario.get("mock_level", "full"),
            "status": "in_progress",
            "start_time": start_iso,
        }
        try:
            (evidence_dir / "run-meta.json").write_text(json.dumps(run_meta, indent=2))
        except Exception as exc:
            logger.warning("Failed to write run-meta.json: %s", exc)

        # Resolve template variables
        template_vars = self._build_template_vars(persona, run_id)

        # Load steps (template resolution happens per-step in the execution loop
        # so that captured_vars from earlier API steps are available)
        steps = scenario.get("steps", [])

        # Check preconditions
        preconditions = scenario.get("preconditions", [])
        precond_ok, precond_errors = self._check_preconditions(preconditions, product_config)

        findings: list[Finding] = []
        step_reports: list[StepReport] = []
        start_time = time.monotonic()

        if not precond_ok:
            for err in precond_errors:
                findings.append(
                    Finding(
                        severity="block",
                        category="server_error",
                        description=f"Precondition failed: {err}",
                        evidence="",
                        step_id="precondition",
                    )
                )
            # Don't run steps if preconditions failed
            end_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            duration = round(time.monotonic() - start_time, 2)
            cost_summary = cost_tracker.get_summary()
            fail_vp_name = viewport_override or persona.get("preferred_device", "desktop")
            fail_vp = product_config.get("viewports", {}).get(fail_vp_name, {"width": 0, "height": 0})

            result = VTERunResult(
                run_id=run_id,
                scenario_name=scenario_name,
                scenario_id=scenario_id,
                product_name=product_config.get("name", product),
                persona_name=persona.get("name", "Unknown"),
                persona_role=persona.get("demographics", {}).get("role", "Unknown"),
                viewport_name=fail_vp_name,
                viewport_size=(fail_vp.get("width", 0), fail_vp.get("height", 0)),
                mock_level=scenario.get("mock_level", "full"),
                passed=False,
                start_time=start_iso,
                end_time=end_iso,
                duration_seconds=duration,
                step_reports=step_reports,
                findings=findings,
                cost_usd=cost_summary.total_cost_usd,
                cost_summary=dataclasses.asdict(cost_summary),
            )
            report = self._report_generator.generate(result)

            # Save structured result as JSON and markdown
            self._save_run_artifacts(result, report, evidence_dir)

            return report, False

        # Initialize runners
        services = product_config.get("services", {})
        backend_url = services.get("backend", {}).get("url", "http://localhost:3001")
        frontend_url = services.get("frontend", {}).get("url", "http://localhost:3000")

        captured_vars: dict[str, Any] = {}

        # Import runners lazily to avoid circular imports and to allow
        # the engine to be used without all runner dependencies installed.
        from ghostqa.engine.api_runner import APIRunner
        from ghostqa.engine.browser_runner import BrowserRunner

        api_runner = APIRunner(
            base_url=backend_url,
            captured_vars=captured_vars,
            scenarios_root=self._scenarios_root,
            product=product,
        )

        # Single browser runner instance reused across all browser steps.
        # Created lazily on the first browser step, closed after all steps finish.
        browser_runner: BrowserRunner | None = None

        # Native app runner -- created lazily on first native_app step.
        native_app_runner: Any = None

        # iOS simulator runner -- created lazily on first ios_simulator step.
        simulator_runner: Any = None

        # Execute steps
        budget_exceeded = False
        max_run_duration = self._config.timeout
        try:
            for step in steps:
                if budget_exceeded:
                    break

                # Global run timeout check
                run_elapsed = time.monotonic() - start_time
                if run_elapsed > max_run_duration:
                    logger.warning(
                        "Run timeout exceeded: %.0fs > %ds limit",
                        run_elapsed,
                        max_run_duration,
                    )
                    findings.append(
                        Finding(
                            severity="block",
                            category="performance",
                            description=f"Run timeout exceeded ({max_run_duration}s)",
                            evidence="",
                            step_id="global_timeout",
                        )
                    )
                    break

                # Per-step template resolution — includes captured vars from earlier API steps
                step_template_vars = {**template_vars, **captured_vars}
                step = self._resolve_templates(step, step_template_vars)

                step_id = step.get("id", "unknown")
                mode = step.get("mode", "api")
                description = step.get("description", "")

                logger.info("Executing step: %s (mode=%s)", step_id, mode)

                try:
                    if mode == "api":
                        api_result = api_runner.execute_step(step)
                        step_reports.append(api_runner.to_step_report(api_result, description))
                        findings.extend(api_result.findings)

                    elif mode == "browser":
                        # Apply viewport override if specified (copy step to avoid mutation)
                        if viewport_override:
                            step = {**step, "viewport": viewport_override}

                        # Lazily create a single BrowserRunner for all browser steps
                        if browser_runner is None:
                            browser_runner = BrowserRunner(
                                frontend_url=frontend_url,
                                product_config=product_config,
                                persona=persona,
                                cost_tracker=cost_tracker,
                                evidence_dir=evidence_dir,
                                api_cookies=api_runner.cookies,
                            )
                            browser_runner.start()

                        browser_result = browser_runner.execute_step(step, captured_vars)
                        step_reports.append(browser_runner.to_step_report(browser_result, description))
                        findings.extend(browser_result.findings)

                    elif mode == "native_app":
                        # Lazily create a NativeAppRunner on first native_app step
                        if native_app_runner is None:
                            from ghostqa.engine.native_app_runner import NativeAppRunner

                            app_path = product_config.get("app_path", "")
                            bundle_id = product_config.get("bundle_id")
                            if not app_path:
                                raise RuntimeError(
                                    "native_app step requires 'app_path' in product config"
                                )
                            native_app_runner = NativeAppRunner(
                                app_path=app_path,
                                evidence_dir=evidence_dir,
                                bundle_id=bundle_id,
                                product_config=product_config,
                            )
                            native_app_runner.start()

                        native_result = native_app_runner.execute_step(step, captured_vars)
                        step_reports.append(
                            native_app_runner.to_step_report(native_result, description)
                        )
                        findings.extend(native_result.findings)

                    elif mode == "ios_simulator":
                        # Lazily create a SimulatorRunner on first ios_simulator step
                        if simulator_runner is None:
                            from ghostqa.engine.simulator_runner import SimulatorRunner

                            bundle_id = product_config.get("bundle_id", "")
                            sim_app_path = product_config.get("app_path")
                            sim_device = product_config.get("simulator_device")
                            sim_os = product_config.get("simulator_os")
                            if not bundle_id:
                                raise RuntimeError(
                                    "ios_simulator step requires 'bundle_id' in product config"
                                )
                            simulator_runner = SimulatorRunner(
                                bundle_id=bundle_id,
                                evidence_dir=evidence_dir,
                                app_path=sim_app_path,
                                device_id=sim_device if sim_device and len(sim_device) > 20 else None,
                                device_name=sim_device if sim_device and len(sim_device) <= 20 else None,
                                os_version=sim_os,
                                product_config=product_config,
                            )
                            simulator_runner.start()

                        sim_result = simulator_runner.execute_step(step, captured_vars)
                        step_reports.append(
                            simulator_runner.to_step_report(sim_result, description)
                        )
                        findings.extend(sim_result.findings)

                    else:
                        logger.warning("Unknown step mode: %s for step %s", mode, step_id)
                        step_reports.append(
                            StepReport(
                                step_id=step_id,
                                description=description,
                                mode=mode,
                                passed=False,
                                duration_seconds=0,
                                error=f"Unknown step mode: {mode}",
                            )
                        )

                except BudgetExceededError as exc:
                    budget_exceeded = True
                    findings.append(
                        Finding(
                            severity="block",
                            category="performance",
                            description=f"Budget exceeded during step {step_id}: {exc}",
                            evidence="",
                            step_id=step_id,
                        )
                    )
                    step_reports.append(
                        StepReport(
                            step_id=step_id,
                            description=description,
                            mode=mode,
                            passed=False,
                            duration_seconds=0,
                            error=str(exc),
                        )
                    )

                except Exception as exc:
                    logger.error("Step %s failed with exception: %s", step_id, exc, exc_info=True)
                    findings.append(
                        Finding(
                            severity="block",
                            category="server_error",
                            description=f"Unhandled exception in step {step_id}: {exc}",
                            evidence="",
                            step_id=step_id,
                        )
                    )
                    step_reports.append(
                        StepReport(
                            step_id=step_id,
                            description=description,
                            mode=mode,
                            passed=False,
                            duration_seconds=0,
                            error=str(exc),
                        )
                    )
        finally:
            # Always close runners if they were started
            if browser_runner is not None:
                browser_runner.stop()
            if native_app_runner is not None:
                native_app_runner.stop()
            if simulator_runner is not None:
                simulator_runner.stop()

        # Determine overall pass/fail
        end_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        duration = round(time.monotonic() - start_time, 2)
        cost_summary = cost_tracker.get_summary()

        # Flush costs to system-wide analytics ledger (no-op if path is None)
        CostTracker.flush_to_system_ledger(
            system_ledger_path=self._config.system_ledger_path,
            calls=cost_tracker.calls,
            run_id=run_id,
            scenario_id=scenario_id,
            product=product,
            initiative_id=self._config.initiative_id,
        )

        all_steps_passed = all(s.passed for s in step_reports)
        has_blocking_findings = any(f.severity == "block" for f in findings)
        passed = all_steps_passed and not has_blocking_findings

        # Classify findings against scenario failure_classification
        classified_findings = self._classify_findings(findings, scenario.get("failure_classification", []))

        # Resolve viewport for report
        report_viewport_name = viewport_override or persona.get("preferred_device", "desktop")
        viewports = product_config.get("viewports", {})
        vp = viewports.get(report_viewport_name, {"width": 0, "height": 0})

        result = VTERunResult(
            run_id=run_id,
            scenario_name=scenario_name,
            scenario_id=scenario_id,
            product_name=product_config.get("name", product),
            persona_name=persona.get("name", "Unknown"),
            persona_role=persona.get("demographics", {}).get("role", "Unknown"),
            viewport_name=report_viewport_name,
            viewport_size=(vp.get("width", 0), vp.get("height", 0)),
            mock_level=scenario.get("mock_level", "full"),
            passed=passed,
            start_time=start_iso,
            end_time=end_iso,
            duration_seconds=duration,
            step_reports=step_reports,
            findings=classified_findings,
            cost_usd=cost_summary.total_cost_usd,
            cost_summary=dataclasses.asdict(cost_summary),
        )

        report = self._report_generator.generate(result)

        # Save structured result as JSON and markdown
        self._save_run_artifacts(result, report, evidence_dir)

        return report, passed

    # ── Config Loading ──────────────────────────────────────────────────

    def _load_product_config(self, product: str) -> dict[str, Any] | None:
        """Load product config from products_dir/{product}.yaml or
        products_dir/{product}/_product.yaml.
        """
        # Try flat file first: <products_dir>/<product>.yaml
        path = self._config.products_dir / f"{product}.yaml"
        if not path.is_file():
            # Fall back to directory style: <products_dir>/<product>/_product.yaml
            path = self._config.products_dir / product / "_product.yaml"
        data = self._load_yaml(path)
        if data is None:
            return None
        return data.get("product", data)

    def _load_persona(self, product: str, ref: str) -> dict[str, Any] | None:
        """Load a persona from personas_dir/{ref}.yaml or
        scenarios_root/{product}/personas/{ref}.yaml.
        """
        # Try config-level personas directory first
        path = self._config.personas_dir / f"{ref}.yaml"
        if not path.is_file():
            # Fall back to product-scoped personas
            path = self._scenarios_root / product / "personas" / f"{ref}.yaml"
        data = self._load_yaml(path)
        if data is None:
            return None
        return data.get("persona", data)

    def _load_scenarios(
        self,
        product: str,
        scenario_id: str | None,
        tag: str | None,
    ) -> list[dict[str, Any]]:
        """Load scenario(s) from journeys_dir/ or
        scenarios_root/{product}/journeys/.
        """
        # Try config-level journeys directory first
        journeys_dir = self._config.journeys_dir
        if not journeys_dir.is_dir():
            # Fall back to product-scoped journeys
            journeys_dir = self._scenarios_root / product / "journeys"

        if not journeys_dir.is_dir():
            logger.error("Journeys directory not found: %s", journeys_dir)
            return []

        scenarios: list[dict[str, Any]] = []
        for f in sorted(journeys_dir.glob("*.yaml")):
            data = self._load_yaml(f)
            if data is None:
                continue
            scenario = data.get("scenario", data)

            # Filter by scenario_id
            if scenario_id and scenario.get("id") != scenario_id:
                continue

            # Filter by tag
            if tag and tag not in scenario.get("tags", []):
                continue

            scenarios.append(data)

        return scenarios

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """Load a YAML file. Returns None if file doesn't exist or can't be parsed."""
        if yaml is None:
            logger.error("PyYAML not installed — cannot load %s", path)
            return None
        if not path.is_file():
            logger.error("File not found: %s", path)
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        except Exception as exc:
            logger.error("Failed to parse YAML %s: %s", path, exc)
            return None

    # ── Template Resolution ──────────────────────────────────────────────

    def _build_template_vars(self, persona: dict[str, Any], run_id: str) -> dict[str, Any]:
        """Build the flat template variable dict for {{var.path}} resolution."""
        return {
            "run_id": run_id,
            "persona": persona,
        }

    def _resolve_templates_in_steps(
        self, steps: list[dict[str, Any]], template_vars: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Recursively resolve {{template.var}} in all step values."""
        resolved: list[dict[str, Any]] = []
        for step in steps:
            resolved.append(self._resolve_templates(step, template_vars))
        return resolved

    def _resolve_templates(self, obj: Any, template_vars: dict[str, Any]) -> Any:
        """Recursively resolve {{template.var}} placeholders in a data structure."""
        if isinstance(obj, str):
            return self._resolve_string_template(obj, template_vars)
        elif isinstance(obj, dict):
            return {k: self._resolve_templates(v, template_vars) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_templates(item, template_vars) for item in obj]
        return obj

    def _resolve_string_template(self, s: str, template_vars: dict[str, Any]) -> str:
        """Resolve all {{dotpath}} occurrences in a string.

        Iterates up to 5 times to handle nested templates (e.g., a persona
        credential containing {{run_id}}).
        """

        def replacer(match: re.Match) -> str:
            dotpath = match.group(1).strip()
            value = self._resolve_dotpath(template_vars, dotpath)
            if value is GhostQAOrchestrator._MISSING:
                logger.warning("Unresolved template variable: {{%s}}", dotpath)
                return match.group(0)  # Leave unresolved
            if value is None:
                return ""  # Explicit null → empty string
            return str(value)

        result = s
        for _ in range(5):  # Max 5 resolution passes
            new_result = re.sub(r"\{\{(.+?)\}\}", replacer, result)
            if new_result == result:
                break  # Stable — no more changes
            result = new_result
        return result

    @staticmethod
    def _resolve_dotpath(data: Any, dotpath: str) -> Any:
        """Resolve a dotpath like 'persona.credentials.email' into a nested dict.

        Returns _MISSING sentinel if the path cannot be resolved.
        Returns None if the path exists but the value is None/null.
        """
        parts = dotpath.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    return GhostQAOrchestrator._MISSING
                current = current[part]
            else:
                return GhostQAOrchestrator._MISSING
        return current

    # ── Preconditions ────────────────────────────────────────────────────

    def _check_preconditions(
        self,
        preconditions: list[dict[str, Any]],
        product_config: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Check all preconditions. Returns (all_ok, list_of_errors)."""
        if not preconditions:
            return True, []

        services = product_config.get("services", {})
        errors: list[str] = []

        for precond in preconditions:
            service_name = precond.get("service", "")
            check = precond.get("check", "")
            expected_status = precond.get("expected_status")

            service = services.get(service_name, {})
            base_url = service.get("url", "")

            if not base_url and service_name == "postgres":
                # Postgres check is done via command
                check_command = service.get("check_command", "")
                if check_command:
                    ok = self._check_command(check_command)
                    if not ok:
                        errors.append(f"Service '{service_name}' check failed: {check_command}")
                continue

            if not base_url:
                errors.append(f"Service '{service_name}' has no URL configured")
                continue

            # HTTP health check
            health_endpoint = check if check.startswith("/") else service.get("health_endpoint", "/")
            url = base_url.rstrip("/") + health_endpoint
            try:
                resp = requests.get(url, timeout=10)
                if expected_status and resp.status_code != expected_status:
                    errors.append(
                        f"Service '{service_name}' health check returned {resp.status_code}, "
                        f"expected {expected_status} (URL: {url})"
                    )
            except requests.RequestException as exc:
                errors.append(f"Service '{service_name}' unreachable: {exc} (URL: {url})")

        return len(errors) == 0, errors

    @staticmethod
    def _check_command(command: str) -> bool:
        """Run a shell command and return True if it exits 0."""
        try:
            result = subprocess.run(shlex.split(command), capture_output=True, timeout=10)
            return result.returncode == 0
        except Exception:
            return False

    # ── Finding Classification ───────────────────────────────────────────

    def _classify_findings(
        self,
        findings: list[Finding],
        classification_rules: list[dict[str, Any]],
    ) -> list[Finding]:
        """Apply scenario failure_classification rules to findings.

        If a finding description matches a classification pattern, update its
        severity and category.
        """
        if not classification_rules:
            return findings

        for finding in findings:
            for rule in classification_rules:
                pattern = rule.get("pattern", "")
                if pattern and re.search(pattern, finding.description, re.IGNORECASE):
                    finding.severity = rule.get("severity", finding.severity)
                    finding.category = rule.get("category", finding.category)
                    break  # First match wins

        return findings

    # ── Utilities ────────────────────────────────────────────────────────

    @staticmethod
    def _save_run_artifacts(result: VTERunResult, report: str, evidence_dir: Path) -> None:
        """Save the run result as JSON and markdown artifacts.

        Args:
            result: The structured VTERunResult dataclass
            report: The generated markdown report
            evidence_dir: Directory to save artifacts to
        """

        # Helper function for JSON serialization of non-standard types
        def json_serialize(obj: Any) -> str:
            if isinstance(obj, Path):
                return str(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        # Save structured result as JSON
        try:
            result_dict = dataclasses.asdict(result)
            result_json_path = evidence_dir / "run-result.json"
            result_json_path.write_text(json.dumps(result_dict, indent=2, default=json_serialize))
            logger.info("Saved run result JSON to %s", result_json_path)
        except Exception as exc:
            logger.warning("Failed to save run-result.json: %s", exc)

        # Save markdown report
        try:
            report_path = evidence_dir / "report.md"
            report_path.write_text(report)
            logger.info("Saved markdown report to %s", report_path)
        except Exception as exc:
            logger.warning("Failed to save report.md: %s", exc)

    @staticmethod
    def _generate_run_id() -> str:
        """Generate a unique run ID."""
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        # Add a short random suffix to avoid collisions
        suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
        return f"GQA-RUN-{ts}-{suffix}"
