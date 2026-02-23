"""SpecterQA Report Generator — Produces run report artifacts in markdown format.

Generates structured markdown reports from run results, including step-by-step
results, UX observations, findings, screenshots, performance data, and cost
breakdown.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class Finding:
    """A finding from a run -- represents an issue or observation."""

    severity: str  # block, critical, high, medium, low
    category: str  # server_error, api_contract, performance, behavior, ux, security
    description: str
    evidence: str  # path to screenshot or relevant data
    step_id: str


@dataclasses.dataclass
class StepReport:
    """Report for a single step in a run."""

    step_id: str
    description: str
    mode: str  # api, browser
    passed: bool
    duration_seconds: float
    error: str | None = None
    notes: str = ""
    action_count: int = 0
    screenshots: list[str] = dataclasses.field(default_factory=list)
    ux_observations: list[str] = dataclasses.field(default_factory=list)
    actions_taken: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    model_routing: dict[str, int] = dataclasses.field(default_factory=dict)
    performance_ms: float | None = None


@dataclasses.dataclass
class RunResult:
    """Complete result of a SpecterQA run.

    Also available as ``VTERunResult`` for backward compatibility with
    code ported from the BusinessAtlas VTE.
    """

    run_id: str
    scenario_name: str
    scenario_id: str
    product_name: str
    persona_name: str
    persona_role: str
    viewport_name: str
    viewport_size: tuple[int, int]
    mock_level: str
    passed: bool
    start_time: str
    end_time: str
    duration_seconds: float
    step_reports: list[StepReport]
    findings: list[Finding]
    cost_usd: float
    cost_summary: dict[str, Any]


class ReportGenerator:
    """Generates markdown reports from run results."""

    def generate(self, result: RunResult) -> str:
        """Generate a complete report in markdown format.

        Args:
            result: The RunResult to report on.

        Returns:
            Complete markdown report as a string.
        """
        sections = [
            self._header(result),
            self._summary(result),
            self._step_results_table(result),
            self._ux_observations(result),
            self._findings_table(result),
            self._screenshots_section(result),
            self._performance_section(result),
            self._cost_section(result),
        ]
        return "\n\n".join(s for s in sections if s)

    def _header(self, r: RunResult) -> str:
        verdict = "PASS" if r.passed else "FAIL"
        return (
            f"# SpecterQA Report: {r.scenario_name}\n"
            f"\n"
            f"**Run ID:** {r.run_id}\n"
            f"**Product:** {r.product_name}\n"
            f"**Persona:** {r.persona_name} ({r.persona_role})\n"
            f"**Viewport:** {r.viewport_name} ({r.viewport_size[0]}x{r.viewport_size[1]})\n"
            f"**Mock Level:** {r.mock_level}\n"
            f"**Date:** {r.start_time}\n"
            f"**Verdict:** {verdict}"
        )

    def _summary(self, r: RunResult) -> str:
        passed_count = sum(1 for s in r.step_reports if s.passed)
        total_count = len(r.step_reports)
        total_actions = sum(s.action_count for s in r.step_reports)
        return (
            f"## Summary\n"
            f"- Steps: {passed_count}/{total_count} passed\n"
            f"- Browser actions: {total_actions}\n"
            f"- Duration: {r.duration_seconds:.1f}s\n"
            f"- Cost: ${r.cost_usd:.4f}"
        )

    def _step_results_table(self, r: RunResult) -> str:
        lines = [
            "## Step Results",
            "| Step | Mode | Result | Duration | Notes |",
            "|------|------|--------|----------|-------|",
        ]
        for step in r.step_reports:
            result_str = "PASS" if step.passed else "FAIL"
            notes = step.error or step.notes or ""
            if len(notes) > 80:
                notes = notes[:77] + "..."
            lines.append(f"| {step.step_id} | {step.mode} | {result_str} | {step.duration_seconds:.1f}s | {notes} |")
        return "\n".join(lines)

    def _ux_observations(self, r: RunResult) -> str:
        all_obs: list[tuple[str, str]] = []
        for step in r.step_reports:
            for obs in step.ux_observations:
                all_obs.append((step.step_id, obs))
        if not all_obs:
            return "## UX Observations\n\nNo UX observations recorded."
        lines = ["## UX Observations", ""]
        for step_id, obs in all_obs:
            lines.append(f"- **{step_id}**: {obs}")
        return "\n".join(lines)

    def _findings_table(self, r: RunResult) -> str:
        if not r.findings:
            return "## Findings\n\nNo findings."
        lines = [
            "## Findings",
            "| Severity | Category | Description | Step | Evidence |",
            "|----------|----------|-------------|------|----------|",
        ]
        for f in r.findings:
            desc = f.description
            if len(desc) > 80:
                desc = desc[:77] + "..."
            evidence = f.evidence if f.evidence else "-"
            lines.append(f"| {f.severity} | {f.category} | {desc} | {f.step_id} | {evidence} |")
        return "\n".join(lines)

    def _screenshots_section(self, r: RunResult) -> str:
        all_screenshots: list[tuple[str, str]] = []
        for step in r.step_reports:
            for ss in step.screenshots:
                all_screenshots.append((step.step_id, ss))
        if not all_screenshots:
            return "## Screenshots\n\nNo screenshots captured."
        lines = ["## Screenshots", ""]
        for step_id, path in all_screenshots:
            lines.append(f"- **{step_id}**: `{path}`")
        return "\n".join(lines)

    def _performance_section(self, r: RunResult) -> str:
        perf_steps = [s for s in r.step_reports if s.performance_ms is not None]
        if not perf_steps:
            return "## Performance\n\nNo performance data collected."
        lines = [
            "## Performance",
            "| Step | Duration (ms) |",
            "|------|---------------|",
        ]
        for step in perf_steps:
            lines.append(f"| {step.step_id} | {step.performance_ms:.0f} |")
        return "\n".join(lines)

    def _cost_section(self, r: RunResult) -> str:
        cs = r.cost_summary
        lines = [
            "## Cost Breakdown",
            f"- **Total cost:** ${r.cost_usd:.4f}",
            f"- **Budget limit:** ${cs.get('budget_limit_usd', 0):.2f}",
            f"- **Budget remaining:** ${cs.get('budget_remaining_usd', 0):.4f}",
            f"- **API calls:** {cs.get('call_count', 0)}",
        ]
        calls_by_model = cs.get("calls_by_model", {})
        cost_by_model = cs.get("cost_by_model", {})
        if calls_by_model:
            lines.append("- **By model:**")
            for model, count in calls_by_model.items():
                cost = cost_by_model.get(model, 0.0)
                # Shorten model name for display
                short_name = model.split("-")[1] if "-" in model else model
                lines.append(f"  - {short_name}: {count} calls, ${cost:.4f}")
        return "\n".join(lines)


# Backward-compatible alias used by the orchestrator (ported from VTE).
VTERunResult = RunResult
