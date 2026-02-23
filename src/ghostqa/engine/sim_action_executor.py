"""GhostQA Simulator Action Executor -- bridges federated AI decisions to iOS Simulator.

Implements the ``ActionExecutor`` protocol from ``ghostqa.engine.protocols`` by
translating ``Decision`` objects into ``SimulatorRunner`` method calls.  Uses
screenshot file hashing for ``ui_changed`` detection and times every action for
``duration_ms``.

Requires Xcode and ``xcrun simctl`` -- the simulator must be booted and the
app launched before actions are executed.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ghostqa.engine.protocols import ActionResult, Decision
from ghostqa.engine.simulator_runner import SimulatorRunner

logger = logging.getLogger("ghostqa.engine.sim_action_executor")


class SimActionExecutor:
    """Maps ``Decision`` objects to iOS Simulator actions via ``SimulatorRunner``.

    Wraps an existing ``SimulatorRunner`` instance and delegates action
    execution to the runner's internal methods (``_action_tap``,
    ``_action_type_text``, ``_action_send_key``, ``_action_swipe``).

    For coordinate-based actions (click/tap), the AI is expected to report
    targets like ``"Sign In button at 200, 450"`` -- the executor parses the
    trailing x,y coordinates.

    Usage::

        runner = SimulatorRunner(
            bundle_id="com.example.myapp",
            evidence_dir=Path("/tmp"),
        )
        runner.start()

        executor = SimActionExecutor(runner, evidence_dir=Path("/tmp"))
        result = executor.execute(decision)

        runner.stop()
    """

    def __init__(
        self,
        runner: SimulatorRunner,
        evidence_dir: Path | None = None,
    ) -> None:
        """
        Args:
            runner: A fully initialised ``SimulatorRunner`` (``start()`` must
                have been called already).
            evidence_dir: Directory where screenshots are saved.  Used to
                hash the latest screenshot for ``ui_changed`` detection.
                If ``None``, change detection is skipped.
        """
        self._runner = runner
        self._evidence_dir = evidence_dir
        self._ss_counter = 0  # Monotonic counter for change-detection screenshots

    # -- ActionExecutor protocol ---------------------------------------------

    def execute(self, decision: Decision) -> ActionResult:
        """Execute a single AI decision against the iOS Simulator.

        Routes by ``decision.action`` to the appropriate runner method.
        Never raises on action failure -- captures the error in the
        returned ``ActionResult``.
        """
        action = decision.action.lower().strip()

        # Snapshot a screenshot hash before the action for change detection
        hash_before = self._snapshot_hash("before")

        start = time.monotonic()
        success = False
        error: str | None = None

        try:
            if action in ("click", "tap"):
                coords = self._parse_coordinates(decision.target)
                if coords is None:
                    error = (
                        f"Could not parse coordinates from target: '{decision.target}'. "
                        "Expected format like 'Sign In button at 200, 450'."
                    )
                else:
                    x, y = coords
                    success = self._runner._action_tap(x, y)

            elif action in ("fill", "type"):
                success = self._runner._action_type_text(decision.value)

            elif action in ("keyboard", "key", "press"):
                success = self._runner._action_send_key(decision.value)

            elif action in ("scroll", "swipe"):
                success = self._do_swipe(decision)

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
                "Simulator action '%s' on '%s' failed: %s",
                action,
                decision.target,
                exc,
                exc_info=True,
            )

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        # Determine whether the UI actually changed
        hash_after = self._snapshot_hash("after")
        ui_changed = (
            hash_before != hash_after if (hash_before and hash_after) else True  # Assume changed if we can't detect
        )

        return ActionResult(
            success=success and error is None,
            action=action,
            target=decision.target,
            error=error,
            duration_ms=duration_ms,
            ui_changed=ui_changed,
        )

    # -- Coordinate Parsing --------------------------------------------------

    @staticmethod
    def _parse_coordinates(text: str) -> tuple[float, float] | None:
        """Extract x,y coordinates from a target description.

        The AI reports targets in several formats:

        - ``"Sign In button at 200, 450"``
        - ``"at approximately 200, 450"``
        - ``"(200, 450)"``
        - ``"x=200, y=450"``
        - ``"200, 450"``

        Returns ``(x, y)`` or ``None`` if no coordinates could be parsed.
        """
        if not text:
            return None

        # Pattern 1: Explicit x=NNN, y=NNN
        m = re.search(
            r"x\s*[=:]\s*(\d+(?:\.\d+)?)\s*[,;]\s*y\s*[=:]\s*(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        # Pattern 2: "at [approximately] NNN, NNN"
        m = re.search(
            r"(?:at|approximately|around|near|position)\s+(\d{1,4}(?:\.\d+)?)\s*,\s*(\d{1,4}(?:\.\d+)?)",
            text,
            re.I,
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        # Pattern 3: Parenthesized "(NNN, NNN)"
        m = re.search(
            r"\(\s*(\d{1,4}(?:\.\d+)?)\s*,\s*(\d{1,4}(?:\.\d+)?)\s*\)",
            text,
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        # Pattern 4: Trailing bare "NNN, NNN" at end of string
        m = re.search(
            r"(\d{1,4}(?:\.\d+)?)\s*,\s*(\d{1,4}(?:\.\d+)?)\s*$",
            text,
        )
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            # Sanity check -- reject values that look like version numbers etc.
            if 0 <= x <= 3000 and 0 <= y <= 3000:
                return x, y

        return None

    # -- Swipe / Scroll ------------------------------------------------------

    def _do_swipe(self, decision: Decision) -> bool:
        """Execute a swipe/scroll gesture via the runner's ``_action_swipe``.

        Parses direction from the target/value and translates to start/end
        coordinates.  Default gesture is a vertical swipe in the centre of
        a standard iPhone screen (390x844 points).
        """
        direction_text = (decision.target + " " + decision.value).lower()

        # Default: centre of an iPhone 15 Pro screen
        cx, cy = 195, 422
        distance = 200

        if "up" in direction_text:
            # Swipe up: finger moves upward (scroll content down)
            x1, y1 = cx, cy + distance // 2
            x2, y2 = cx, cy - distance // 2
        elif "left" in direction_text:
            x1, y1 = cx + distance // 2, cy
            x2, y2 = cx - distance // 2, cy
        elif "right" in direction_text:
            x1, y1 = cx - distance // 2, cy
            x2, y2 = cx + distance // 2, cy
        else:
            # Default: swipe down (finger moves downward, scroll content up)
            x1, y1 = cx, cy - distance // 2
            x2, y2 = cx, cy + distance // 2

        return self._runner._action_swipe(x1, y1, x2, y2, duration=0.3)

    # -- Change Detection ----------------------------------------------------

    def _snapshot_hash(self, label: str) -> str:
        """Take a screenshot and return its file hash for change detection.

        Returns an empty string if screenshotting is not possible (e.g. no
        evidence directory or simctl failure).
        """
        if self._evidence_dir is None:
            return ""

        self._ss_counter += 1
        ss_path = self._runner._take_screenshot(
            step_id="chg-detect",
            action_idx=self._ss_counter,
            label=label,
        )
        if ss_path is None:
            return ""

        return SimulatorRunner._hash_file(ss_path)

    # -- Utilities -----------------------------------------------------------

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
