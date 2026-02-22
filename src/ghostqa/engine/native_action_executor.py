"""GhostQA Native Action Executor -- bridges federated AI decisions to macOS AX API.

Implements the ``ActionExecutor`` protocol from ``ghostqa.engine.protocols`` by
translating ``Decision`` objects into ``NativeAppRunner`` method calls.  Captures
accessibility tree hashes before/after each action for ``ui_changed`` detection
and times every action for ``duration_ms``.

Requires pyobjc (``[native]`` extra) -- will raise at construction time if
pyobjc is not available.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ghostqa.engine.native_app_runner import NativeAppRunner
from ghostqa.engine.protocols import ActionResult, Decision

logger = logging.getLogger("ghostqa.engine.native_action_executor")


class NativeActionExecutor:
    """Maps ``Decision`` objects to macOS Accessibility API calls.

    Wraps an existing ``NativeAppRunner`` instance and delegates action
    execution to the runner's internal methods (``_action_click``,
    ``_action_type``, ``_action_key_press``, etc.).

    Usage::

        runner = NativeAppRunner(app_path="/Applications/MyApp.app", evidence_dir=Path("/tmp"))
        runner.start()

        executor = NativeActionExecutor(runner)
        result = executor.execute(decision)

        runner.stop()
    """

    def __init__(self, runner: NativeAppRunner) -> None:
        """
        Args:
            runner: A fully initialised ``NativeAppRunner`` (``start()`` must
                have been called already).
        """
        self._runner = runner

    # -- ActionExecutor protocol ---------------------------------------------

    def execute(self, decision: Decision) -> ActionResult:
        """Execute a single AI decision against the native macOS app.

        Routes by ``decision.action`` to the appropriate runner method.
        Never raises on action failure -- captures the error in the
        returned ``ActionResult``.
        """
        action = decision.action.lower().strip()

        # Snapshot the UI tree hash before the action for change detection
        tree_hash_before = self._hash_tree()

        start = time.monotonic()
        success = False
        error: str | None = None

        try:
            if action in ("click", "tap"):
                success = self._runner._action_click(decision.target, role="")

            elif action in ("fill", "type"):
                success = self._runner._action_type(
                    decision.target,
                    decision.value,
                    role="",
                )

            elif action in ("keyboard", "key", "press"):
                success = self._runner._action_key_press(decision.value)

            elif action == "scroll":
                success = self._do_scroll(decision)

            elif action == "wait":
                wait_secs = self._parse_wait(decision.value)
                time.sleep(wait_secs)
                success = True

            elif action in ("done", "stuck"):
                # No-op -- the step runner interprets these
                success = True

            else:
                error = f"Unknown action type: {action}"

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Native action '%s' on '%s' failed: %s",
                action,
                decision.target,
                exc,
                exc_info=True,
            )

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        # Determine whether the UI actually changed
        tree_hash_after = self._hash_tree()
        ui_changed = tree_hash_before != tree_hash_after

        return ActionResult(
            success=success and error is None,
            action=action,
            target=decision.target,
            error=error,
            duration_ms=duration_ms,
            ui_changed=ui_changed,
        )

    # -- Helpers -------------------------------------------------------------

    def _hash_tree(self) -> str:
        """Capture the current AX tree hash via the runner."""
        try:
            return self._runner._hash_accessibility_tree()
        except Exception:
            return ""

    def _do_scroll(self, decision: Decision) -> bool:
        """Attempt a scroll action via CGEvent.

        macOS does not have a single simple scroll API; we synthesise a
        scroll-wheel event via Quartz CGEvent.  If the required Quartz
        symbols are unavailable we skip gracefully.
        """
        try:
            from Quartz import (  # type: ignore[import-untyped]
                CGEventCreateScrollWheelEvent,
                CGEventPost,
                kCGHIDEventTap,
                kCGScrollEventUnitLine,
            )
        except ImportError:
            logger.warning("Quartz scroll symbols not available -- skipping scroll")
            return False

        # Parse direction from the target / value
        direction_text = (decision.target + " " + decision.value).lower()
        lines = -3  # Default: scroll down (negative = down in Quartz)
        if "up" in direction_text:
            lines = 3
        elif "down" in direction_text:
            lines = -3

        try:
            event = CGEventCreateScrollWheelEvent(
                None,
                kCGScrollEventUnitLine,
                1,  # wheelCount
                lines,
            )
            CGEventPost(kCGHIDEventTap, event)
            time.sleep(0.3)
            return True
        except Exception as exc:
            logger.warning("CGEvent scroll failed: %s", exc)
            return False

    @staticmethod
    def _parse_wait(value: str) -> float:
        """Parse a wait duration from the decision value.

        Accepts numeric strings (seconds) or defaults to 1.0.
        """
        if not value:
            return 1.0
        try:
            return max(0.1, float(value))
        except (ValueError, TypeError):
            return 1.0
