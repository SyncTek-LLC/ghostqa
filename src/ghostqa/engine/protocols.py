"""Federated AI Testing Protocols.

These protocols define the contract between GhostQA's generic AI testing loop
and consumer-provided implementations (VTE PersonaAgent, ForgeOS TestAgent, etc.).
Consumers inject their own AIDecider and the loop handles the rest.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable


@dataclasses.dataclass
class Decision:
    """What the AI decided to do next."""

    action: str  # click, fill, keyboard, scroll, wait, done, stuck
    target: str  # Element label/identifier (for click/fill) or description
    value: str  # Text to type (fill), key name (keyboard), seconds (wait)
    reasoning: str  # Why this action was chosen
    goal_achieved: bool  # True when the step goal is complete
    observation: str = ""  # What the agent sees on screen
    ux_notes: str | None = None  # UX issues spotted
    checkpoint: str | None = None  # Milestone reached


@dataclasses.dataclass
class ActionResult:
    """Result of executing a single action."""

    success: bool
    action: str
    target: str
    error: str | None = None
    duration_ms: float = 0.0
    ui_changed: bool = True  # Whether the UI state changed after action


@dataclasses.dataclass
class StepResult:
    """Result of executing an AI-driven step."""

    step_id: str
    passed: bool
    screenshots: list[str]
    ux_observations: list[str]
    actions_taken: list[dict[str, Any]]
    action_count: int
    duration_seconds: float
    checkpoints_reached: list[str]
    findings: list[Any]  # Finding objects
    error: str | None = None
    goal_achieved: bool = False


@runtime_checkable
class AIDecider(Protocol):
    """AI brain -- receives screenshot + context, returns action decision.

    Consumers implement this with their own AI model (Claude, GPT, local, etc.).
    The AIStepRunner calls decide() in a loop until goal_achieved or max iterations.
    """

    def decide(
        self,
        goal: str,
        screenshot_base64: str,
        ui_context: str = "",
        force_api: bool = False,
        stuck_context: str | None = None,
        **kwargs: Any,
    ) -> Decision: ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Maps AI decisions to platform-specific UI actions.

    NativeActionExecutor maps to macOS AX API.
    SimActionExecutor maps to iOS Simulator simctl.
    BrowserActionExecutor maps to Playwright.
    """

    def execute(self, decision: Decision) -> ActionResult: ...
