"""SpecterQA Native App Runner -- macOS native application testing.

Launches macOS applications, interacts with them via the Accessibility API
(AXUIElement through pyobjc-framework-ApplicationServices), captures
window-specific screenshots via ``screencapture``, reads UI state from the
accessibility tree, and performs actions (click, type, key press).

Includes stuck detection by hashing the accessibility tree between actions.

pyobjc dependencies are conditionally imported so that SpecterQA continues to
work on systems where pyobjc is not installed (e.g. Linux CI, or macOS
without the ``[native]`` extra).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from specterqa.engine.report_generator import Finding, StepReport

logger = logging.getLogger("specterqa.engine.native_app_runner")

# ---------------------------------------------------------------------------
# Conditional pyobjc imports
# ---------------------------------------------------------------------------
_HAS_PYOBJC = False

try:
    from ApplicationServices import (  # type: ignore[import-untyped]
        AXIsProcessTrusted,
        AXUIElementCopyAttributeNames,  # noqa: F401 — pyobjc bridge may need at runtime
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        AXValueGetValue,
        kAXErrorSuccess,
    )

    # Constants from HIServices/AXValue.h — used to unpack AXValueRef objects
    _kAXValueCGPointType = 1  # noqa: N816 — matches Apple SDK naming convention
    _kAXValueCGSizeType = 2  # noqa: N816 — matches Apple SDK naming convention

    from Cocoa import (  # type: ignore[import-untyped]
        NSRunningApplication,  # noqa: F401 — pyobjc bridge may need at runtime
        NSWorkspace,
    )
    from Quartz import (  # type: ignore[import-untyped]
        CGEventCreateKeyboardEvent,
        CGEventCreateMouseEvent,
        CGEventPost,
        CGEventSetIntegerValueField,
        CGPoint,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
        kCGMouseEventClickState,
    )

    _HAS_PYOBJC = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class NativeAppStepResult:
    """Result of executing a single native-app step."""

    step_id: str
    passed: bool
    screenshots: list[str]
    ux_observations: list[str]
    actions_taken: list[dict[str, Any]]
    action_count: int
    duration_seconds: float
    findings: list[Finding]
    error: str | None = None
    goal_achieved: bool = False


@dataclasses.dataclass
class UIElement:
    """Lightweight representation of an AXUIElement node."""

    role: str
    title: str
    value: str | None
    description: str
    identifier: str
    placeholder: str
    position: tuple[float, float] | None
    size: tuple[float, float] | None
    enabled: bool
    children_count: int
    # Internal reference -- not serialised
    _ref: Any = dataclasses.field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Key-code mapping (subset of macOS virtual key codes)
# ---------------------------------------------------------------------------

_KEY_CODES: dict[str, int] = {
    "return": 0x24,
    "enter": 0x24,
    "tab": 0x30,
    "space": 0x31,
    "delete": 0x33,
    "backspace": 0x33,
    "escape": 0x35,
    "esc": 0x35,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
    "a": 0x00,
    "b": 0x0B,
    "c": 0x08,
    "d": 0x02,
    "e": 0x0E,
    "f": 0x03,
    "g": 0x05,
    "h": 0x04,
    "i": 0x22,
    "j": 0x26,
    "k": 0x28,
    "l": 0x25,
    "m": 0x2E,
    "n": 0x2D,
    "o": 0x1F,
    "p": 0x23,
    "q": 0x0C,
    "r": 0x0F,
    "s": 0x01,
    "t": 0x11,
    "u": 0x20,
    "v": 0x09,
    "w": 0x0D,
    "x": 0x07,
    "y": 0x10,
    "z": 0x06,
}


# ---------------------------------------------------------------------------
# NativeAppRunner
# ---------------------------------------------------------------------------


class NativeAppRunner:
    """Executes native macOS app steps from scenario definitions.

    Manages the application lifecycle (launch / activate), reads UI state
    via the Accessibility API, performs actions, captures screenshots, and
    implements stuck detection.

    Usage::

        runner = NativeAppRunner(
            app_path="/Applications/MyApp.app",
            evidence_dir=Path("/tmp/evidence"),
        )
        runner.start()
        result = runner.execute_step(step_dict)
        runner.stop()
    """

    def __init__(
        self,
        app_path: str,
        evidence_dir: Path,
        bundle_id: str | None = None,
        product_config: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            app_path: Path to the .app bundle (e.g. ``/Applications/MyApp.app``).
            evidence_dir: Directory for saving screenshots and evidence.
            bundle_id: Optional bundle identifier (e.g. ``com.example.myapp``).
                If not supplied it is inferred from the running app.
            product_config: Product configuration dict (optional).
        """
        if not _HAS_PYOBJC:
            raise RuntimeError(
                "pyobjc is required for native macOS app testing. Install it with: pip install 'specterqa[native]'"
            )

        self._app_path = app_path
        self._evidence_dir = Path(evidence_dir)
        self._bundle_id = bundle_id
        self._product_config = product_config or {}

        # Runtime state -- populated by start()
        self._pid: int | None = None
        self._app_ref: Any = None  # AXUIElement for the application
        self._window_id: int | None = None
        self._wrapped_bundle_dir: Path | None = None  # Temp .app bundle for bare executables

    # -- Lifecycle -----------------------------------------------------------

    def _wrap_bare_executable(self, exe_path: str) -> str:
        """Wrap a bare Mach-O executable in a temporary .app bundle.

        macOS Accessibility API requires a bundle identifier to inspect a
        process.  Bare executables (e.g. SPM build output) lack one, causing
        error -25204 (kAXErrorCannotComplete).  This method creates a minimal
        ``.app`` bundle with an ``Info.plist`` and a symlink to the original
        binary so the AX API can work.

        Returns the path to the generated ``.app`` bundle.
        """
        exe = Path(exe_path).resolve()
        app_name = exe.stem

        bundle_base = Path("/tmp/specterqa_bundles")
        bundle_base.mkdir(parents=True, exist_ok=True)
        bundle_dir = bundle_base / f"{app_name}.app"

        # Clean up any stale bundle from a previous run
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)

        macos_dir = bundle_dir / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)

        # Symlink the actual binary into the bundle
        (macos_dir / app_name).symlink_to(exe)

        # Derive a bundle ID
        bundle_id = self._bundle_id or f"com.specterqa.wrapped.{app_name.lower()}"
        self._bundle_id = bundle_id

        # Write Info.plist
        info_plist = {
            "CFBundleIdentifier": bundle_id,
            "CFBundleName": app_name,
            "CFBundleExecutable": app_name,
            "CFBundlePackageType": "APPL",
            "NSHighResolutionCapable": True,
        }
        plist_path = bundle_dir / "Contents" / "Info.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump(info_plist, f)

        self._wrapped_bundle_dir = bundle_dir
        logger.info(
            "Wrapping bare executable in .app bundle for accessibility API access: %s -> %s",
            exe_path,
            bundle_dir,
        )
        return str(bundle_dir)

    def start(self) -> None:
        """Launch (or activate) the app and obtain an AXUIElement reference.

        If *app_path* points to a bare executable (not a ``.app`` bundle), it
        is automatically wrapped in a temporary ``.app`` bundle so the
        macOS Accessibility API can inspect it.

        Raises ``RuntimeError`` if the Accessibility API is not trusted or the
        app cannot be found after launch.
        """
        if not AXIsProcessTrusted():
            raise RuntimeError(
                "Accessibility API not trusted. Grant Terminal / IDE access "
                "in System Settings > Privacy & Security > Accessibility."
            )

        # Auto-wrap bare executables in a .app bundle
        launch_path = self._app_path
        if not launch_path.endswith(".app"):
            exe = Path(launch_path)
            if exe.is_file():
                launch_path = self._wrap_bare_executable(launch_path)
                self._app_path = launch_path

        # Extract bundle ID from existing .app bundle if not already set
        if not self._bundle_id and self._app_path.endswith(".app"):
            plist_path = Path(self._app_path) / "Contents" / "Info.plist"
            if plist_path.exists():
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                self._bundle_id = plist.get("CFBundleIdentifier", "")

        # Launch the application
        logger.info("Launching native app: %s", self._app_path)
        subprocess.run(["open", "-g", "-a", self._app_path], check=True, timeout=30)

        # Wait for the application to appear
        self._pid = self._wait_for_app(timeout=15)
        if self._pid is None:
            raise RuntimeError(f"App did not launch within timeout: {self._app_path}")

        self._app_ref = AXUIElementCreateApplication(self._pid)
        self._window_id = self._get_main_window_id()

        logger.info(
            "Native app running: pid=%s, window_id=%s",
            self._pid,
            self._window_id,
        )

    def stop(self) -> None:
        """Terminate the application if it was launched by this runner.

        Also cleans up any temporary ``.app`` bundle created for bare
        executables.
        """
        if self._pid is not None:
            try:
                subprocess.run(
                    ["kill", str(self._pid)],
                    capture_output=True,
                    timeout=10,
                )
                logger.info("Terminated native app pid=%s", self._pid)
            except Exception as exc:
                logger.warning("Failed to terminate app pid=%s: %s", self._pid, exc)

        # Clean up temporary .app bundle
        if self._wrapped_bundle_dir is not None:
            try:
                shutil.rmtree(self._wrapped_bundle_dir)
                logger.info("Cleaned up temporary bundle: %s", self._wrapped_bundle_dir)
            except Exception as exc:
                logger.warning("Failed to clean up temporary bundle %s: %s", self._wrapped_bundle_dir, exc)
            self._wrapped_bundle_dir = None

        self._pid = None
        self._app_ref = None
        self._window_id = None

    # -- Step Execution ------------------------------------------------------

    def execute_step(
        self,
        step: dict[str, Any],
        captured_vars: dict[str, Any] | None = None,
    ) -> NativeAppStepResult:
        """Execute a single native-app step.

        Args:
            step: Step definition dict from the scenario YAML.
            captured_vars: Variables captured from previous steps (unused for
                native steps but kept for interface consistency).

        Returns:
            NativeAppStepResult with screenshots, actions, pass/fail.
        """
        step_id = step.get("id", "unknown")
        goal = step.get("goal", "")
        max_actions = step.get("max_actions", 20)
        max_duration = step.get("max_duration_seconds", 120)
        actions_spec = step.get("actions", [])

        logger.info("Native-app step %s: goal=%s, %d scripted actions", step_id, goal[:80], len(actions_spec))

        screenshots: list[str] = []
        ux_observations: list[str] = []
        actions_taken: list[dict[str, Any]] = []
        findings: list[Finding] = []
        goal_achieved = False
        error_msg: str | None = None

        start_time = time.monotonic()

        # Stuck detection
        prev_tree_hash: str | None = None
        consecutive_stuck = 0
        max_stuck = 5

        action_idx = 0
        for action_spec in actions_spec:
            if action_idx >= max_actions:
                error_msg = f"Max actions ({max_actions}) reached"
                break

            elapsed = time.monotonic() - start_time
            if elapsed > max_duration:
                error_msg = f"Step timed out after {elapsed:.0f}s (limit: {max_duration}s)"
                findings.append(
                    Finding(
                        severity="high",
                        category="performance",
                        description=error_msg,
                        evidence="",
                        step_id=step_id,
                    )
                )
                break

            # Take screenshot before action
            ss_path = self._take_screenshot(step_id, action_idx, "before")
            if ss_path:
                screenshots.append(ss_path)

            # Stuck detection via accessibility tree hash
            tree_hash = self._hash_accessibility_tree()
            if tree_hash == prev_tree_hash:
                consecutive_stuck += 1
            else:
                consecutive_stuck = 0
            prev_tree_hash = tree_hash

            if consecutive_stuck >= max_stuck:
                error_msg = f"App stuck: no UI change for {consecutive_stuck} actions"
                findings.append(
                    Finding(
                        severity="critical",
                        category="ux",
                        description=f"agent_stuck: {error_msg}",
                        evidence=ss_path or "",
                        step_id=step_id,
                    )
                )
                break

            # Execute the action
            action_type = action_spec.get("action", "")
            target = action_spec.get("target", "")
            value = action_spec.get("value", "")
            role = action_spec.get("role", "")

            action_start = time.monotonic()
            success = False
            action_error: str | None = None

            try:
                if action_type == "click":
                    success = self._action_click(target, role)
                elif action_type == "type":
                    success = self._action_type(target, value, role)
                elif action_type == "key":
                    success = self._action_key_press(value)
                elif action_type == "wait":
                    wait_secs = float(value) if value else 1.0
                    time.sleep(wait_secs)
                    success = True
                elif action_type == "done":
                    goal_achieved = True
                    success = True
                else:
                    action_error = f"Unknown action type: {action_type}"
            except Exception as exc:
                action_error = str(exc)
                logger.error("Action %s failed: %s", action_type, exc, exc_info=True)

            action_duration = time.monotonic() - action_start

            actions_taken.append(
                {
                    "index": action_idx,
                    "action": action_type,
                    "target": target,
                    "value": value,
                    "success": success,
                    "error": action_error,
                    "duration_ms": round(action_duration * 1000, 1),
                }
            )

            if action_error:
                findings.append(
                    Finding(
                        severity="high",
                        category="ux",
                        description=f"Action '{action_type}' on '{target}' failed: {action_error}",
                        evidence=ss_path or "",
                        step_id=step_id,
                    )
                )

            if goal_achieved:
                break

            # Brief pause to let the UI settle
            time.sleep(0.3)
            action_idx += 1

        # Take final screenshot
        final_ss = self._take_screenshot(step_id, action_idx, "final")
        if final_ss:
            screenshots.append(final_ss)

        if not goal_achieved and error_msg is None:
            error_msg = "All scripted actions completed but goal not explicitly achieved"

        duration = round(time.monotonic() - start_time, 2)
        passed = goal_achieved and error_msg is None

        return NativeAppStepResult(
            step_id=step_id,
            passed=passed,
            screenshots=screenshots,
            ux_observations=ux_observations,
            actions_taken=actions_taken,
            action_count=action_idx,
            duration_seconds=duration,
            findings=findings,
            error=error_msg,
            goal_achieved=goal_achieved,
        )

    def to_step_report(self, result: NativeAppStepResult, description: str = "") -> StepReport:
        """Convert a NativeAppStepResult into a generic StepReport."""
        return StepReport(
            step_id=result.step_id,
            description=description,
            mode="native_app",
            passed=result.passed,
            duration_seconds=result.duration_seconds,
            error=result.error,
            notes=f"{result.action_count} actions, {'goal achieved' if result.goal_achieved else 'goal NOT achieved'}",
            action_count=result.action_count,
            screenshots=result.screenshots,
            ux_observations=result.ux_observations,
            actions_taken=result.actions_taken,
        )

    # -- Accessibility Tree --------------------------------------------------

    def _get_ax_attribute(self, element: Any, attr: str) -> Any:
        """Safely read an accessibility attribute from an AXUIElement.

        Returns ``None`` if the attribute does not exist or cannot be read.
        """
        try:
            err, value = AXUIElementCopyAttributeValue(element, attr, None)
            if err == kAXErrorSuccess:
                return value
        except Exception:
            pass
        return None

    def _get_element_info(self, element: Any) -> UIElement:
        """Build a UIElement from an AXUIElement reference."""
        role = str(self._get_ax_attribute(element, "AXRole") or "")
        title = str(self._get_ax_attribute(element, "AXTitle") or "")
        value = self._get_ax_attribute(element, "AXValue")
        if value is not None:
            value = str(value)

        description = str(self._get_ax_attribute(element, "AXDescription") or "")
        identifier = str(self._get_ax_attribute(element, "AXIdentifier") or "")
        placeholder = str(self._get_ax_attribute(element, "AXPlaceholderValue") or "")

        enabled_raw = self._get_ax_attribute(element, "AXEnabled")
        enabled = bool(enabled_raw) if enabled_raw is not None else True

        position = None
        pos_raw = self._get_ax_attribute(element, "AXPosition")
        if pos_raw is not None:
            try:
                ok, point = AXValueGetValue(pos_raw, _kAXValueCGPointType, None)
                if ok and point is not None:
                    position = (float(point.x), float(point.y))
            except Exception:
                # Fallback for environments where AXValueGetValue is unavailable
                try:
                    position = (float(pos_raw.x), float(pos_raw.y))
                except Exception:
                    pass

        size = None
        size_raw = self._get_ax_attribute(element, "AXSize")
        if size_raw is not None:
            try:
                ok, sz = AXValueGetValue(size_raw, _kAXValueCGSizeType, None)
                if ok and sz is not None:
                    size = (float(sz.width), float(sz.height))
            except Exception:
                # Fallback for environments where AXValueGetValue is unavailable
                try:
                    size = (float(size_raw.width), float(size_raw.height))
                except Exception:
                    pass

        children = self._get_ax_attribute(element, "AXChildren") or []
        children_count = len(children) if hasattr(children, "__len__") else 0

        return UIElement(
            role=role,
            title=title,
            value=value,
            description=description,
            identifier=identifier,
            placeholder=placeholder,
            position=position,
            size=size,
            enabled=enabled,
            children_count=children_count,
            _ref=element,
        )

    def _traverse_tree(
        self,
        element: Any,
        max_depth: int = 15,
        _depth: int = 0,
    ) -> list[UIElement]:
        """Recursively traverse the accessibility tree rooted at *element*.

        Returns a flat list of UIElement objects.
        """
        if _depth > max_depth:
            return []

        results: list[UIElement] = []

        info = self._get_element_info(element)
        results.append(info)

        children = self._get_ax_attribute(element, "AXChildren") or []
        if hasattr(children, "__iter__"):
            for child in children:
                results.extend(self._traverse_tree(child, max_depth, _depth + 1))

        return results

    def _find_element(
        self,
        target: str,
        role: str = "",
    ) -> UIElement | None:
        """Search the accessibility tree for an element matching *target* and
        optionally *role*.

        Search priority:
        1. Exact substring match on title
        2. Exact substring match on description (AXDescription)
        3. Exact substring match on identifier (AXIdentifier)
        4. Exact substring match on placeholder (AXPlaceholderValue)
        5. Exact substring match on value (AXValue)
        6. Fuzzy word match across all text fields
        """
        if self._app_ref is None:
            return None

        elements = self._traverse_tree(self._app_ref)
        target_lower = target.lower()

        def _role_ok(el: UIElement) -> bool:
            return (not role) or role.lower() in el.role.lower()

        # Priority 1: exact substring match on title
        for el in elements:
            if _role_ok(el) and el.title and target_lower in el.title.lower():
                return el

        # Priority 2: exact substring match on description
        for el in elements:
            if _role_ok(el) and el.description and target_lower in el.description.lower():
                return el

        # Priority 3: exact substring match on identifier
        for el in elements:
            if _role_ok(el) and el.identifier and target_lower in el.identifier.lower():
                return el

        # Priority 4: exact substring match on placeholder
        for el in elements:
            if _role_ok(el) and el.placeholder and target_lower in el.placeholder.lower():
                return el

        # Priority 5: exact substring match on value
        for el in elements:
            if _role_ok(el) and el.value and target_lower in el.value.lower():
                return el

        # Priority 6: fuzzy word match across all text fields
        target_words = set(target_lower.split())
        best: UIElement | None = None
        best_score = 0
        for el in elements:
            if not _role_ok(el):
                continue
            el_text = " ".join(
                s for s in [el.title, el.description, el.identifier, el.placeholder, el.value or ""] if s
            ).lower()
            if not el_text.strip():
                continue
            score = sum(1 for w in target_words if w in el_text)
            if score > best_score:
                best_score = score
                best = el

        if best_score > 0:
            return best

        return None

    def _find_element_at_coordinates(self, x: float, y: float) -> UIElement | None:
        """Find the accessibility element at screen coordinates (x, y).

        Traverses the AX tree and finds the smallest element whose bounding
        box contains the point. Prefers interactive elements (buttons, text
        fields) over containers (groups, windows).
        """
        if self._app_ref is None:
            return None

        elements = self._traverse_tree(self._app_ref)

        # Filter to elements that contain the point
        containing: list[UIElement] = []
        for el in elements:
            if el.position is None or el.size is None:
                continue
            ex, ey = el.position
            ew, eh = el.size
            if ex <= x <= ex + ew and ey <= y <= ey + eh:
                containing.append(el)

        if not containing:
            return None

        # Prefer interactive elements (buttons, text fields, etc.)
        interactive_roles = {
            "AXButton",
            "AXTextField",
            "AXTextArea",
            "AXCheckBox",
            "AXRadioButton",
            "AXPopUpButton",
            "AXComboBox",
            "AXSlider",
            "AXLink",
            "AXMenuItem",
            "AXTab",
        }

        interactive = [el for el in containing if el.role in interactive_roles]
        if interactive:
            # Return the smallest interactive element (most specific)
            return min(
                interactive,
                key=lambda el: (el.size[0] * el.size[1]) if el.size else float("inf"),
            )

        # Fallback: smallest containing element
        return min(
            containing,
            key=lambda el: (el.size[0] * el.size[1]) if el.size else float("inf"),
        )

    def _hash_accessibility_tree(self) -> str:
        """Hash the current accessibility tree for stuck detection.

        Returns a hex digest string representing the current UI state.
        """
        if self._app_ref is None:
            return ""
        try:
            elements = self._traverse_tree(self._app_ref, max_depth=8)
            tree_repr = json.dumps(
                [
                    {
                        "role": e.role,
                        "title": e.title,
                        "description": e.description,
                        "value": e.value,
                        "enabled": e.enabled,
                    }
                    for e in elements
                ],
                sort_keys=True,
            )
            return hashlib.sha256(tree_repr.encode()).hexdigest()[:16]
        except Exception as exc:
            logger.warning("Failed to hash accessibility tree: %s", exc)
            return ""

    # -- Actions -------------------------------------------------------------

    def _action_click(self, target: str, role: str = "") -> bool:
        """Click an element identified by *target* (title substring or coordinates).

        Uses AXPress (process-level, works in background) as the primary
        mechanism.  Falls back to AXConfirm as an alternative AX action.
        Does NOT use CGEvent coordinate-based clicks (those steal focus).

        If *target* contains coordinates (e.g. ``"195,420"`` or
        ``"button at 195, 420"``), the runner will attempt to resolve
        them to the nearest AX element when text-based search fails.
        """
        # Try to extract coordinates from the target string
        coord_match = re.search(r"(\d+)\s*,\s*(\d+)", target)

        # First try text-based search
        element = self._find_element(target, role)

        # If text search fails and we have coordinates, try coordinate resolution
        if element is None and coord_match:
            x, y = float(coord_match.group(1)), float(coord_match.group(2))
            element = self._find_element_at_coordinates(x, y)
            if element:
                logger.info(
                    "Resolved coordinates (%d,%d) to element: role=%s title='%s'",
                    x,
                    y,
                    element.role,
                    element.title,
                )

        if element is None:
            logger.warning("Click target not found: '%s' (role=%s)", target, role)
            return False

        if element._ref is not None:
            # Try AXPress action (process-level, no focus steal)
            try:
                err = AXUIElementPerformAction(element._ref, "AXPress")
                if err == kAXErrorSuccess:
                    logger.debug("AXPress succeeded on '%s'", target)
                    return True
            except Exception:
                pass

            # Try AXConfirm as an alternative AX action
            try:
                err = AXUIElementPerformAction(element._ref, "AXConfirm")
                if err == kAXErrorSuccess:
                    logger.debug("AXConfirm succeeded on '%s'", target)
                    return True
            except Exception:
                pass

        logger.warning(
            "Cannot click '%s': AXPress and AXConfirm both failed (no CGEvent fallback — background mode)",
            target,
        )
        return False

    def _action_type(self, target: str, value: str, role: str = "") -> bool:
        """Type text into a field via the Accessibility API.

        Approach 1 (preferred): Set AXValue directly on the element via
        ``AXUIElementSetAttributeValue``.  This works for NSTextField and
        NSTextView (backing SwiftUI TextField / TextEditor) without
        requiring the app to be frontmost.

        Approach 2 (fallback): Clipboard paste via process-targeted
        AppleScript (does NOT activate the app first).
        """
        element = self._find_element(target, role)
        if element is None:
            logger.warning("Type target not found: '%s'", target)
            return False

        # -- Approach 1: AXSetValue (no focus required) -----------------------
        if element._ref is not None:
            try:
                err = AXUIElementSetAttributeValue(element._ref, "AXValue", value)
                if err == kAXErrorSuccess:
                    logger.debug("AXSetValue succeeded for '%s' (%d chars)", target, len(value))
                    return True
                else:
                    logger.debug("AXSetValue returned error %s for '%s'", err, target)
            except Exception as exc:
                logger.debug("AXSetValue failed for '%s': %s", target, exc)

        # -- Approach 2: Clipboard paste via targeted AppleScript (no app activation)
        app_name = Path(self._app_path).stem
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore[import-untyped]

            pb = NSPasteboard.generalPasteboard()
            old_contents = pb.stringForType_(NSPasteboardTypeString)

            pb.clearContents()
            pb.setString_forType_(value, NSPasteboardTypeString)
            time.sleep(0.05)

            # Select all in current field and paste — targets the specific
            # process without bringing it to the foreground.
            script = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        keystroke "a" using command down
                        delay 0.05
                        keystroke "v" using command down
                    end tell
                end tell
            '''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                # Wait for app to process the paste event before touching clipboard
                time.sleep(0.5)
                # Restore clipboard
                if old_contents:
                    pb.clearContents()
                    pb.setString_forType_(old_contents, NSPasteboardTypeString)
                logger.debug("AppleScript paste into '%s' succeeded (%d chars)", target, len(value))
                return True
            else:
                logger.debug("AppleScript paste failed: %s", result.stderr.strip())
        except Exception as exc:
            logger.debug("Paste approach failed: %s", exc)

        logger.warning("All type approaches failed for '%s'", target)
        return False

    def _action_key_press(self, key_name: str) -> bool:
        """Press a named key or key combo (e.g. ``return``, ``cmd+n``, ``shift+tab``).

        Supports modifier+key combinations separated by ``+``:
        - ``cmd`` / ``command`` -> Command
        - ``shift`` -> Shift
        - ``opt`` / ``option`` / ``alt`` -> Option
        - ``ctrl`` / ``control`` -> Control

        Uses process-targeted AppleScript ``key code`` to send the key
        event to the specific application without requiring it to be
        frontmost.  For return/enter without modifiers, tries
        AXPress/AXConfirm on the focused element first (pure AX, no
        focus steal).

        Returns True if the key was sent successfully.
        """
        key_lower = key_name.lower().strip()

        # Parse modifier+key combos (e.g. "cmd+n", "shift+tab", "cmd+shift+s")
        modifiers: list[str] = []
        key_part = key_lower
        if "+" in key_lower:
            parts = [p.strip() for p in key_lower.split("+")]
            key_part = parts[-1]  # Last part is the actual key
            for mod in parts[:-1]:
                if mod in ("cmd", "command"):
                    modifiers.append("command down")
                elif mod == "shift":
                    modifiers.append("shift down")
                elif mod in ("opt", "option", "alt"):
                    modifiers.append("option down")
                elif mod in ("ctrl", "control"):
                    modifiers.append("control down")

        key_code = _KEY_CODES.get(key_part)
        if key_code is None:
            logger.warning("Unknown key name: '%s' (parsed key_part='%s')", key_name, key_part)
            return False

        # For return/enter without modifiers, try AX actions on the focused element first
        if key_part in ("return", "enter") and not modifiers and self._app_ref is not None:
            focused = self._get_ax_attribute(self._app_ref, "AXFocusedUIElement")
            if focused is not None:
                try:
                    err = AXUIElementPerformAction(focused, "AXPress")
                    if err == kAXErrorSuccess:
                        logger.debug("AXPress on focused element succeeded for '%s'", key_name)
                        return True
                except Exception:
                    pass
                try:
                    err = AXUIElementPerformAction(focused, "AXConfirm")
                    if err == kAXErrorSuccess:
                        logger.debug("AXConfirm on focused element succeeded for '%s'", key_name)
                        return True
                except Exception:
                    pass

        # Process-targeted AppleScript key code (with optional modifiers)
        app_name = Path(self._app_path).stem
        if modifiers:
            modifier_str = " using {" + ", ".join(modifiers) + "}"
            script = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        key code {key_code}{modifier_str}
                    end tell
                end tell
            '''
        else:
            script = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        key code {key_code}
                    end tell
                end tell
            '''
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.debug(
                    "AppleScript key code %d%s sent to '%s'",
                    key_code,
                    f" with modifiers [{', '.join(modifiers)}]" if modifiers else "",
                    app_name,
                )
                return True
            else:
                logger.warning(
                    "AppleScript key press failed for '%s': %s",
                    key_name,
                    result.stderr.strip(),
                )
                return False
        except Exception as exc:
            logger.warning("Key press '%s' failed: %s", key_name, exc)
            return False

    def _click_at(self, x: float, y: float) -> bool:
        """Send a mouse-click event at screen coordinates (x, y).

        .. deprecated::
            This method uses CGEvent which operates at the screen level,
            moves the physical mouse cursor, and requires the target window
            to be frontmost.  It is retained for backward compatibility but
            is no longer called by any action method.  Use AXPress instead.
        """
        try:
            point = CGPoint(x, y)
            event_down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
            event_up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
            CGEventSetIntegerValueField(event_down, kCGMouseEventClickState, 1)
            CGEventSetIntegerValueField(event_up, kCGMouseEventClickState, 1)
            CGEventPost(kCGHIDEventTap, event_down)
            CGEventPost(kCGHIDEventTap, event_up)
            time.sleep(0.1)
            return True
        except Exception as exc:
            logger.warning("Coordinate click at (%.0f, %.0f) failed: %s", x, y, exc)
            return False

    def _send_key_event(self, key_code: int, shift: bool = False) -> None:
        """Post a keyboard event for the given virtual key code.

        .. deprecated::
            This method uses CGEvent which posts events to whichever
            application is frontmost.  It is retained for backward
            compatibility but is no longer called by any action method.
            Use process-targeted AppleScript ``key code`` instead.
        """
        try:
            event_down = CGEventCreateKeyboardEvent(None, key_code, True)
            event_up = CGEventCreateKeyboardEvent(None, key_code, False)
            if shift:
                # Set shift flag (0x20000 = kCGEventFlagMaskShift)
                from Quartz import kCGEventFlagMaskShift  # type: ignore[import-untyped]

                CGEventSetIntegerValueField(event_down, 0, kCGEventFlagMaskShift)
                CGEventSetIntegerValueField(event_up, 0, kCGEventFlagMaskShift)
            CGEventPost(kCGHIDEventTap, event_down)
            CGEventPost(kCGHIDEventTap, event_up)
            time.sleep(0.05)
        except Exception as exc:
            logger.warning("Key event (code=%d) failed: %s", key_code, exc)

    @staticmethod
    def _applescript_keystroke(char: str) -> None:
        """Send a single character via AppleScript as a last resort.

        .. deprecated::
            This method sends untargeted keystrokes to whichever app is
            frontmost.  It is retained for backward compatibility but is
            no longer called by ``_action_type()``.
        """
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "System Events" to keystroke "{char}"',
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            logger.warning("AppleScript keystroke '%s' failed: %s", char, exc)

    # -- Window ID Refresh ---------------------------------------------------

    def _refresh_window_id(self) -> None:
        """Re-query the main window ID for the app's PID.

        Window IDs become stale when:
        - The app is restarted
        - A window is closed and recreated
        - The app opens new windows (sheets, dialogs, preferences)

        This method uses ``CGWindowListCopyWindowInfo`` to find the current
        main window for our PID and updates ``self._window_id``.  First
        tries on-screen windows, then falls back to all windows (including
        background) for resilience when the app is launched with ``open -g``.
        If no window is found at all the cached ID is left unchanged (the
        screenshot fallback in ``_take_screenshot`` will handle it).
        """
        if self._pid is None:
            return

        try:
            from Quartz import (  # type: ignore[import-untyped]
                CGWindowListCopyWindowInfo,
                kCGNullWindowID,
                kCGWindowListOptionAll,
                kCGWindowListOptionOnScreenOnly,
            )

            window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)

            # Collect all on-screen windows for our PID, preferring the
            # frontmost (lowest kCGWindowLayer, then highest kCGWindowNumber
            # as a tie-breaker for the most recently created window).
            candidates: list[tuple[int, float, int]] = []  # (layer, area, wid)
            for win in window_list:
                if win.get("kCGWindowOwnerPID") == self._pid:
                    wid = win.get("kCGWindowNumber")
                    layer = win.get("kCGWindowLayer", 0)
                    # Filter out tiny windows (menu bar, toolbar, status items)
                    bounds = win.get("kCGWindowBounds", {})
                    w = bounds.get("Width", 0)
                    h = bounds.get("Height", 0)
                    if wid and h >= 50 and w >= 50:
                        candidates.append((int(layer), float(w * h), int(wid)))

            # Fallback: try all windows if no on-screen candidates found
            if not candidates:
                logger.debug(
                    "No on-screen windows for pid=%s, trying kCGWindowListOptionAll",
                    self._pid,
                )
                window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
                for win in window_list:
                    if win.get("kCGWindowOwnerPID") == self._pid:
                        wid = win.get("kCGWindowNumber")
                        layer = win.get("kCGWindowLayer", 0)
                        # Filter out tiny windows (menu bar, toolbar, status items)
                        bounds = win.get("kCGWindowBounds", {})
                        w = bounds.get("Width", 0)
                        h = bounds.get("Height", 0)
                        if wid and h >= 50 and w >= 50:
                            candidates.append((int(layer), float(w * h), int(wid)))

            if candidates:
                logger.debug(
                    "Window candidates for pid=%s: %s",
                    self._pid,
                    [(f"wid={c[2]}", f"area={c[1]}", f"layer={c[0]}") for c in candidates],
                )
                # Sort by layer ascending (0 = normal windows), then by area
                # descending (largest first), then wid descending (newest as
                # tie-breaker).
                candidates.sort(key=lambda c: (c[0], -c[1], -c[2]))
                new_wid = candidates[0][2]
                if new_wid != self._window_id:
                    logger.info(
                        "Window ID refreshed: %s -> %s (pid=%s, %d candidate(s))",
                        self._window_id,
                        new_wid,
                        self._pid,
                        len(candidates),
                    )
                    self._window_id = new_wid
        except Exception as exc:
            logger.warning("Failed to refresh window ID: %s", exc)

    # -- Screenshot ----------------------------------------------------------

    def _take_screenshot(
        self,
        step_id: str,
        action_idx: int,
        label: str,
    ) -> str | None:
        """Take a screenshot of the app window using ``screencapture``.

        Before capturing, refreshes the window ID to avoid stale references.
        If window-specific capture fails, falls back to full-screen capture.

        Returns the file path, or None on failure.
        """
        # Refresh window ID to avoid stale references (Bug fix: window IDs
        # become invalid when the app recreates windows or opens dialogs)
        self._refresh_window_id()

        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{step_id}-{action_idx:03d}-{label}.png"
        filepath = self._evidence_dir / filename

        cmd: list[str]
        if self._window_id is not None:
            # Window-specific capture
            cmd = ["screencapture", "-l", str(self._window_id), "-x", str(filepath)]
        else:
            # Full-screen fallback
            cmd = ["screencapture", "-x", str(filepath)]

        try:
            subprocess.run(cmd, capture_output=True, timeout=10, check=True)
            logger.debug("Screenshot saved: %s", filepath)
        except Exception as exc:
            logger.warning("Window capture command failed: %s", exc)

        # Check if the captured image is too small (menu bar capture).
        # A 48px-tall PNG is a menu-bar-only capture (24 CSS points @ 2x).
        # Delete it so the fallback logic below re-captures full-screen.
        if filepath.exists() and filepath.stat().st_size > 0 and self._window_id is not None:
            try:
                result = subprocess.run(
                    ["sips", "--getProperty", "pixelHeight", str(filepath)],
                    capture_output=True,
                    timeout=5,
                    text=True,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "pixelHeight" in line:
                            height = int(line.split(":")[-1].strip())
                            if height < 200:  # 100 CSS points at @2x
                                logger.warning(
                                    "Window capture too small (%dpx tall) for WID %s — falling back to full-screen",
                                    height,
                                    self._window_id,
                                )
                                filepath.unlink(exist_ok=True)  # Remove bad capture
                                break
            except Exception:
                pass  # Don't fail on size check

        # If window-specific capture failed (exit code 1, empty file, or no
        # file at all), fall back to full-screen capture.  This prevents
        # sending empty base64 strings to the API.
        if not filepath.exists() or filepath.stat().st_size == 0:
            logger.warning(
                "Window capture failed for WID %s — falling back to full-screen",
                self._window_id,
            )
            try:
                cmd = ["screencapture", "-x", str(filepath)]
                subprocess.run(cmd, capture_output=True, timeout=10, check=True)
            except Exception as exc:
                logger.warning("Full-screen screenshot fallback also failed: %s", exc)
                return None

        # Final check — if we still have no usable file, give up
        if not filepath.exists() or filepath.stat().st_size == 0:
            logger.warning("Screenshot file missing or empty after all attempts: %s", filepath)
            return None

        # -- Post-capture compression ------------------------------------------
        # macOS screencapture on Retina displays produces @2x PNGs (5-13 MB)
        # that exceed the Anthropic API 5 MB image limit. Use macOS built-in
        # ``sips`` to resize and/or convert so the file stays under 4 MB
        # (leaving margin below the 5 MB hard limit).
        _MAX_BYTES = 4 * 1024 * 1024  # 4 MB threshold  # noqa: N806 — constant naming convention

        try:
            size = filepath.stat().st_size
            if size > _MAX_BYTES:
                logger.info(
                    "Screenshot %s is %.1f MB — resizing with sips",
                    filepath.name,
                    size / (1024 * 1024),
                )
                subprocess.run(
                    ["sips", "--resampleWidth", "1440", str(filepath)],
                    capture_output=True,
                    timeout=15,
                    check=True,
                )
                size = filepath.stat().st_size

            if size > _MAX_BYTES:
                # Still too large — convert to JPEG at quality 80
                jpg_path = filepath.with_suffix(".jpg")
                logger.info(
                    "Screenshot still %.1f MB after resize — converting to JPEG",
                    size / (1024 * 1024),
                )
                subprocess.run(
                    [
                        "sips",
                        "-s",
                        "format",
                        "jpeg",
                        "-s",
                        "formatOptions",
                        "80",
                        str(filepath),
                        "--out",
                        str(jpg_path),
                    ],
                    capture_output=True,
                    timeout=15,
                    check=True,
                )
                # Remove the oversized PNG and return the JPEG path
                filepath.unlink(missing_ok=True)
                logger.info(
                    "Compressed screenshot: %s (%.1f MB)",
                    jpg_path.name,
                    jpg_path.stat().st_size / (1024 * 1024),
                )
                return str(jpg_path)
        except Exception as exc:
            # Compression is best-effort — return whatever we have
            logger.warning("Screenshot compression failed: %s", exc)

        return str(filepath)

    # -- App Discovery -------------------------------------------------------

    def _wait_for_app(self, timeout: int = 15) -> int | None:
        """Wait for the application to appear in the running process list.

        When ``self._bundle_id`` is set (e.g. for wrapped executables), bundle
        ID matching takes priority over name matching to avoid collisions with
        stale processes that share the same app name.

        When multiple processes share the same bundle ID (e.g. a zombie from a
        previous build alongside a freshly launched instance), the process with
        the highest PID is preferred because it is the most recently spawned.
        Terminated processes are always skipped.

        Returns the PID, or None if the app does not appear within *timeout*
        seconds.
        """
        app_name = Path(self._app_path).stem
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            apps = NSWorkspace.sharedWorkspace().runningApplications()

            # Priority 1: match by bundle ID when we know it
            if self._bundle_id:
                candidates: list[int] = []
                for app in apps:
                    if app.isTerminated():
                        continue
                    bid = app.bundleIdentifier()
                    if bid and str(bid) == self._bundle_id:
                        candidates.append(app.processIdentifier())
                if candidates:
                    best_pid = max(candidates)
                    if len(candidates) > 1:
                        logger.info(
                            "Multiple processes for bundle %s (PIDs: %s), choosing highest: %d",
                            self._bundle_id,
                            candidates,
                            best_pid,
                        )
                    return best_pid

            # Priority 2: match by app name
            for app in apps:
                if app.isTerminated():
                    continue
                name = app.localizedName()
                bid = app.bundleIdentifier()
                if name and app_name.lower() in name.lower():
                    self._bundle_id = self._bundle_id or str(bid or "")
                    return app.processIdentifier()

            time.sleep(0.5)

        return None

    def _get_main_window_id(self) -> int | None:
        """Retrieve the CGWindowID for the main window of the app.

        Uses ``CGWindowListCopyWindowInfo`` to find the window belonging
        to the app's PID.  First tries on-screen windows, then falls back
        to all windows (including background) for resilience when the app
        is launched with ``open -g``.
        """
        if self._pid is None:
            return None
        try:
            from Quartz import (  # type: ignore[import-untyped]
                CGWindowListCopyWindowInfo,
                kCGNullWindowID,
                kCGWindowListOptionAll,
                kCGWindowListOptionOnScreenOnly,
            )

            # Try on-screen windows first
            window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
            for win in window_list:
                if win.get("kCGWindowOwnerPID") == self._pid:
                    wid = win.get("kCGWindowNumber")
                    if wid:
                        return int(wid)

            # Fallback: include all windows (background apps)
            logger.debug(
                "No on-screen window for pid=%s, trying kCGWindowListOptionAll",
                self._pid,
            )
            window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
            for win in window_list:
                if win.get("kCGWindowOwnerPID") == self._pid:
                    wid = win.get("kCGWindowNumber")
                    if wid:
                        return int(wid)
        except Exception as exc:
            logger.warning("Failed to get window ID: %s", exc)

        return None

    # -- Space Isolation (aspirational) --------------------------------------

    def _create_isolated_space(self) -> bool:
        """Move the app window to a new macOS Space for visual isolation.

        Uses AppleScript via Mission Control to:
        1. Open Mission Control
        2. Create a new Space (click the '+' button)
        3. Move the app window to that Space

        This is best-effort.  It requires Mission Control accessibility
        permissions and may not work on all macOS versions.  Failures are
        logged but do not prevent test execution.

        Returns True if the window was (likely) moved, False otherwise.
        """
        if self._pid is None:
            logger.warning("Cannot create isolated space: no PID")
            return False

        app_name = Path(self._app_path).stem
        script = f'''
            -- Open Mission Control, add a space, and move the app there
            tell application "Mission Control" to launch
            delay 1.0

            tell application "System Events"
                -- Click the '+' button to add a new Desktop/Space
                -- (This appears in the Spaces bar at the top of Mission Control)
                try
                    click button 1 of group "Spaces Bar" of group 1 of process "Dock"
                on error
                    -- Fallback: try finding the add-space button by description
                    try
                        click (first button whose description is "add desktop") of ¬
                            group "Spaces Bar" of group 1 of process "Dock"
                    end try
                end try
                delay 0.5

                -- Press Escape to exit Mission Control
                key code 53
                delay 0.5
            end tell

            -- Move the app window to the new (last) space
            -- We do this by setting the app to full screen briefly, then reverting,
            -- or by using the window's context menu in Mission Control.
            -- Simpler approach: use the 'move window' gesture via System Events
            tell application "System Events"
                tell process "{app_name}"
                    try
                        set frontmost to true
                    end try
                end tell
            end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info("Isolated space created for %s", app_name)
                # Refresh window ID since the window may have been recreated
                self._refresh_window_id()
                return True
            else:
                logger.warning(
                    "Failed to create isolated space: %s",
                    result.stderr.strip(),
                )
                return False
        except Exception as exc:
            logger.warning("Isolated space creation failed: %s", exc)
            return False

    # -- UI State Dump (for debugging) ---------------------------------------

    def dump_ui_tree(self) -> list[dict[str, Any]]:
        """Return the current accessibility tree as a list of dicts.

        Useful for debugging and for AI persona agents to understand the
        current UI state.
        """
        if self._app_ref is None:
            return []

        elements = self._traverse_tree(self._app_ref, max_depth=10)
        return [
            {
                "role": e.role,
                "title": e.title,
                "description": e.description,
                "identifier": e.identifier,
                "placeholder": e.placeholder,
                "value": e.value,
                "position": e.position,
                "size": e.size,
                "enabled": e.enabled,
                "children_count": e.children_count,
            }
            for e in elements
        ]
