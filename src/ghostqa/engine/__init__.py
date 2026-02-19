"""GhostQA engine — core testing modules.

Provides the complete behavioral testing engine:
- PersonaAgent: AI persona that interprets screenshots and decides actions
- BrowserRunner: Playwright browser orchestration with stuck detection
- ActionExecutor: Translates persona decisions into browser commands
- APIRunner: HTTP API step execution with validation and variable capture
- ReportGenerator: Markdown report generation from run results
- MockServer: Contract-driven HTTP mock server
- CostTracker: AI token cost tracking and budget enforcement
"""

from ghostqa.engine.action_executor import ActionExecutor, ActionResult, PersonaDecision
from ghostqa.engine.browser_runner import BrowserRunner, BrowserStepResult
from ghostqa.engine.cost_tracker import (
    BudgetExceededError,
    CostTracker,
    CumulativeBudgetExceededError,
)
from ghostqa.engine.persona_agent import AgentStuckError, PersonaAgent
from ghostqa.engine.report_generator import Finding, ReportGenerator, RunResult, StepReport

__all__ = [
    "ActionExecutor",
    "ActionResult",
    "AgentStuckError",
    "BrowserRunner",
    "BrowserStepResult",
    "BudgetExceededError",
    "CostTracker",
    "CumulativeBudgetExceededError",
    "Finding",
    "PersonaAgent",
    "PersonaDecision",
    "ReportGenerator",
    "RunResult",
    "StepReport",
]
