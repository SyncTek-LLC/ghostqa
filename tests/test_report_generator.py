"""Unit tests for specterqa.engine.report_generator — Finding, StepReport, RunResult, ReportGenerator."""

from __future__ import annotations

import dataclasses

import pytest

from specterqa.engine.report_generator import (
    Finding,
    ReportGenerator,
    RunResult,
    StepReport,
)


# ---------------------------------------------------------------------------
# Helpers — factory functions for test data
# ---------------------------------------------------------------------------

def _make_finding(**overrides) -> Finding:
    defaults = {
        "severity": "medium",
        "category": "ux",
        "description": "Button is hard to find",
        "evidence": "screenshots/step1.png",
        "step_id": "step_login",
    }
    defaults.update(overrides)
    return Finding(**defaults)


def _make_step_report(**overrides) -> StepReport:
    defaults = {
        "step_id": "step_1",
        "description": "Navigate to homepage",
        "mode": "browser",
        "passed": True,
        "duration_seconds": 5.2,
    }
    defaults.update(overrides)
    return StepReport(**defaults)


def _make_run_result(**overrides) -> RunResult:
    defaults = {
        "run_id": "RUN-TEST-001",
        "scenario_name": "Onboarding Happy Path",
        "scenario_id": "onboarding-happy",
        "product_name": "testapp",
        "persona_name": "alex_tester",
        "persona_role": "QA Engineer",
        "viewport_name": "desktop",
        "viewport_size": (1280, 720),
        "mock_level": "none",
        "passed": True,
        "start_time": "2026-02-19T10:00:00+00:00",
        "end_time": "2026-02-19T10:05:00+00:00",
        "duration_seconds": 300.0,
        "step_reports": [],
        "findings": [],
        "cost_usd": 0.50,
        "cost_summary": {
            "budget_limit_usd": 5.00,
            "budget_remaining_usd": 4.50,
            "call_count": 10,
            "calls_by_model": {},
            "cost_by_model": {},
        },
    }
    defaults.update(overrides)
    return RunResult(**defaults)


# ---------------------------------------------------------------------------
# 1. Finding dataclass
# ---------------------------------------------------------------------------

class TestFinding:
    """Finding dataclass should store all required fields."""

    def test_finding_fields(self):
        f = _make_finding()
        assert f.severity == "medium"
        assert f.category == "ux"
        assert f.description == "Button is hard to find"
        assert f.evidence == "screenshots/step1.png"
        assert f.step_id == "step_login"

    def test_finding_is_dataclass(self):
        assert dataclasses.is_dataclass(Finding)

    def test_finding_all_severities(self):
        for sev in ("block", "critical", "high", "medium", "low"):
            f = _make_finding(severity=sev)
            assert f.severity == sev


# ---------------------------------------------------------------------------
# 2. StepReport defaults
# ---------------------------------------------------------------------------

class TestStepReport:
    """StepReport should have sensible defaults for optional fields."""

    def test_default_error_is_none(self):
        sr = _make_step_report()
        assert sr.error is None

    def test_default_notes_is_empty(self):
        sr = _make_step_report()
        assert sr.notes == ""

    def test_default_action_count_is_zero(self):
        sr = _make_step_report()
        assert sr.action_count == 0

    def test_default_screenshots_is_empty_list(self):
        sr = _make_step_report()
        assert sr.screenshots == []

    def test_default_ux_observations_is_empty_list(self):
        sr = _make_step_report()
        assert sr.ux_observations == []

    def test_default_actions_taken_is_empty_list(self):
        sr = _make_step_report()
        assert sr.actions_taken == []

    def test_default_model_routing_is_empty_dict(self):
        sr = _make_step_report()
        assert sr.model_routing == {}

    def test_default_performance_ms_is_none(self):
        sr = _make_step_report()
        assert sr.performance_ms is None

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(StepReport)


# ---------------------------------------------------------------------------
# 3. RunResult computed properties
# ---------------------------------------------------------------------------

class TestRunResult:
    """RunResult should correctly store all fields."""

    def test_all_fields_present(self):
        rr = _make_run_result()
        assert rr.run_id == "RUN-TEST-001"
        assert rr.scenario_name == "Onboarding Happy Path"
        assert rr.product_name == "testapp"
        assert rr.passed is True

    def test_passed_is_true_or_false(self):
        rr_pass = _make_run_result(passed=True)
        rr_fail = _make_run_result(passed=False)
        assert rr_pass.passed is True
        assert rr_fail.passed is False

    def test_viewport_size_tuple(self):
        rr = _make_run_result(viewport_size=(1920, 1080))
        assert rr.viewport_size == (1920, 1080)

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RunResult)

    def test_step_reports_list(self):
        steps = [_make_step_report(step_id="s1"), _make_step_report(step_id="s2")]
        rr = _make_run_result(step_reports=steps)
        assert len(rr.step_reports) == 2

    def test_findings_list(self):
        findings = [_make_finding(), _make_finding(severity="critical")]
        rr = _make_run_result(findings=findings)
        assert len(rr.findings) == 2


# ---------------------------------------------------------------------------
# 4. ReportGenerator.generate() — markdown output
# ---------------------------------------------------------------------------

class TestReportGenerator:
    """ReportGenerator should produce well-formed markdown reports."""

    def test_generate_contains_header(self):
        rr = _make_run_result()
        report = ReportGenerator().generate(rr)
        assert "# SpecterQA Report:" in report
        assert rr.scenario_name in report
        assert rr.run_id in report

    def test_generate_contains_verdict_pass(self):
        rr = _make_run_result(passed=True)
        report = ReportGenerator().generate(rr)
        assert "**Verdict:** PASS" in report

    def test_generate_contains_verdict_fail(self):
        rr = _make_run_result(passed=False)
        report = ReportGenerator().generate(rr)
        assert "**Verdict:** FAIL" in report

    def test_generate_contains_summary_section(self):
        steps = [
            _make_step_report(step_id="s1", passed=True, action_count=5),
            _make_step_report(step_id="s2", passed=False, action_count=3),
        ]
        rr = _make_run_result(step_reports=steps, cost_usd=1.2345)
        report = ReportGenerator().generate(rr)
        assert "## Summary" in report
        assert "1/2 passed" in report
        assert "8" in report  # total actions (5+3)
        assert "$1.2345" in report

    def test_generate_contains_step_results_table(self):
        steps = [_make_step_report(step_id="login", mode="browser", passed=True)]
        rr = _make_run_result(step_reports=steps)
        report = ReportGenerator().generate(rr)
        assert "## Step Results" in report
        assert "| login |" in report
        assert "PASS" in report

    def test_generate_findings_section_when_present(self):
        findings = [_make_finding(severity="critical", description="Server returned 500")]
        rr = _make_run_result(findings=findings)
        report = ReportGenerator().generate(rr)
        assert "## Findings" in report
        assert "critical" in report
        assert "Server returned 500" in report

    def test_generate_findings_section_when_empty(self):
        rr = _make_run_result(findings=[])
        report = ReportGenerator().generate(rr)
        assert "No findings." in report

    def test_generate_ux_observations(self):
        steps = [_make_step_report(
            step_id="s1",
            ux_observations=["Loading spinner is not visible"],
        )]
        rr = _make_run_result(step_reports=steps)
        report = ReportGenerator().generate(rr)
        assert "## UX Observations" in report
        assert "Loading spinner is not visible" in report

    def test_generate_screenshots_section(self):
        steps = [_make_step_report(step_id="s1", screenshots=["evidence/s1_001.png"])]
        rr = _make_run_result(step_reports=steps)
        report = ReportGenerator().generate(rr)
        assert "## Screenshots" in report
        assert "evidence/s1_001.png" in report

    def test_generate_performance_section(self):
        steps = [_make_step_report(step_id="s1", performance_ms=1234.5)]
        rr = _make_run_result(step_reports=steps)
        report = ReportGenerator().generate(rr)
        assert "## Performance" in report
        assert "1234" in report  # truncated to int in format

    def test_generate_cost_section(self):
        rr = _make_run_result(cost_usd=2.5)
        report = ReportGenerator().generate(rr)
        assert "## Cost Breakdown" in report
        assert "$2.5" in report

    def test_generate_returns_string(self):
        rr = _make_run_result()
        report = ReportGenerator().generate(rr)
        assert isinstance(report, str)

    def test_generate_long_description_truncated_in_findings(self):
        long_desc = "A" * 200
        findings = [_make_finding(description=long_desc)]
        rr = _make_run_result(findings=findings)
        report = ReportGenerator().generate(rr)
        assert "..." in report
        # Full 200-char description should not appear verbatim
        assert long_desc not in report
