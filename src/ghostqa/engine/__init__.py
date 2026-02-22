"""GhostQA engine — core testing modules.

Provides the complete behavioral testing engine:
- PersonaAgent: AI persona that interprets screenshots and decides actions
- BrowserRunner: Playwright browser orchestration with stuck detection
- NativeAppRunner: macOS native app testing via Accessibility API (requires ``[native]`` extra)
- SimulatorRunner: iOS Simulator testing via ``xcrun simctl`` (requires Xcode)
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

# NativeAppRunner and SimulatorRunner are NOT eagerly imported here because
# they depend on platform-specific packages (pyobjc, Xcode).  Import them
# directly from their modules when needed:
#   from ghostqa.engine.native_app_runner import NativeAppRunner
#   from ghostqa.engine.simulator_runner import SimulatorRunner

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
    # Platform-specific runners (import from their modules directly):
    # "NativeAppRunner",
    # "SimulatorRunner",
]
