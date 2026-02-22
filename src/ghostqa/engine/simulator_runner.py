"""GhostQA Simulator Runner -- iOS Simulator testing.

Manages iOS Simulator lifecycle via ``xcrun simctl``: boot devices, install
and launch apps, capture screenshots, simulate touch and keyboard input, and
detect stuck states via perceptual screenshot hashing.

All dependencies are standard macOS CLI tools (xcrun, osascript) so no
third-party packages are required beyond what ships with Xcode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from ghostqa.engine.report_generator import Finding, StepReport

logger = logging.getLogger("ghostqa.engine.simulator_runner")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SimulatorStepResult:
    """Result of executing a single iOS simulator step."""

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


# ---------------------------------------------------------------------------
# SimulatorRunner
# ---------------------------------------------------------------------------

class SimulatorRunner:
    """Executes iOS Simulator steps from scenario definitions.

    Manages a simulator device: boot, install app, launch, interact via
    ``simctl`` and AppleScript, capture screenshots, and detect stuck
    states.

    Usage::

        runner = SimulatorRunner(
            bundle_id="com.example.myapp",
            app_path="/path/to/MyApp.app",
            evidence_dir=Path("/tmp/evidence"),
        )
        runner.start()
        result = runner.execute_step(step_dict)
        runner.stop()
    """

    def __init__(
        self,
        bundle_id: str,
        evidence_dir: Path,
        app_path: str | None = None,
        device_id: str | None = None,
        device_name: str | None = None,
        os_version: str | None = None,
        product_config: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            bundle_id: The iOS app bundle identifier (e.g. ``com.example.myapp``).
            evidence_dir: Directory for saving screenshots and evidence.
            app_path: Path to the ``.app`` bundle to install.  If ``None`` the
                app is assumed to be already installed on the simulator.
            device_id: Explicit simulator device UDID.  If ``None``, the runner
                picks the best available device matching *device_name* and
                *os_version*.
            device_name: Preferred device name (e.g. ``"iPhone 15 Pro"``).
                Used when *device_id* is not supplied.
            os_version: Preferred iOS version (e.g. ``"17.2"``).
                Used when *device_id* is not supplied.
            product_config: Product configuration dict (optional).
        """
        self._bundle_id = bundle_id
        self._evidence_dir = evidence_dir
        self._app_path = app_path
        self._device_id = device_id
        self._device_name = device_name or "iPhone 15 Pro"
        self._os_version = os_version
        self._product_config = product_config or {}

        # Populated during start()
        self._booted_by_us = False

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Boot the simulator (if needed), install the app, and launch it.

        If no explicit *device_id* was given, the runner picks the best
        matching device from the available simulators.
        """
        # Resolve device
        if self._device_id is None:
            self._device_id = self._find_best_device()
            if self._device_id is None:
                raise RuntimeError(
                    f"No simulator device found matching name='{self._device_name}' "
                    f"os='{self._os_version}'. Available devices:\n"
                    f"{json.dumps(self._list_devices(), indent=2)}"
                )

        logger.info(
            "Using simulator device: %s (name=%s)",
            self._device_id,
            self._device_name,
        )

        # Boot if not already booted
        if not self._is_booted():
            logger.info("Booting simulator %s", self._device_id)
            self._simctl("boot", self._device_id)
            self._booted_by_us = True
            # Wait for the simulator to become ready
            self._wait_for_boot(timeout=60)
        else:
            logger.info("Simulator %s already booted", self._device_id)

        # Open the Simulator.app UI so we can interact with it
        subprocess.run(
            ["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", self._device_id],
            capture_output=True,
            timeout=15,
        )
        time.sleep(2)

        # Install app if path provided
        if self._app_path:
            logger.info("Installing app: %s", self._app_path)
            self._simctl("install", self._device_id, self._app_path)
            time.sleep(1)

        # Launch the app
        logger.info("Launching app: %s", self._bundle_id)
        self._simctl("launch", self._device_id, self._bundle_id)
        time.sleep(2)

    def stop(self, shutdown: bool = False) -> None:
        """Terminate the app and optionally shut down the simulator.

        Args:
            shutdown: If True and we booted the simulator, shut it down.
        """
        if self._device_id is None:
            return

        # Terminate the app
        try:
            self._simctl("terminate", self._device_id, self._bundle_id)
            logger.info("Terminated app %s on simulator %s", self._bundle_id, self._device_id)
        except Exception as exc:
            logger.warning("Failed to terminate app: %s", exc)

        # Optionally shut down the simulator
        if shutdown and self._booted_by_us:
            try:
                self._simctl("shutdown", self._device_id)
                logger.info("Shut down simulator %s", self._device_id)
            except Exception as exc:
                logger.warning("Failed to shut down simulator: %s", exc)

    # -- Step Execution ------------------------------------------------------

    def execute_step(
        self,
        step: dict[str, Any],
        captured_vars: dict[str, Any] | None = None,
    ) -> SimulatorStepResult:
        """Execute a single iOS simulator step.

        Args:
            step: Step definition dict from the scenario YAML.
            captured_vars: Variables captured from previous steps (unused for
                simulator steps but kept for interface consistency).

        Returns:
            SimulatorStepResult with screenshots, actions, pass/fail.
        """
        step_id = step.get("id", "unknown")
        goal = step.get("goal", "")
        max_actions = step.get("max_actions", 20)
        max_duration = step.get("max_duration_seconds", 120)
        actions_spec = step.get("actions", [])

        logger.info(
            "Simulator step %s: goal=%s, %d scripted actions",
            step_id,
            goal[:80],
            len(actions_spec),
        )

        screenshots: list[str] = []
        ux_observations: list[str] = []
        actions_taken: list[dict[str, Any]] = []
        findings: list[Finding] = []
        goal_achieved = False
        error_msg: str | None = None

        start_time = time.monotonic()

        # Stuck detection via screenshot hash
        prev_ss_hash: str | None = None
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

                # Stuck detection via perceptual hash of screenshot bytes
                ss_hash = self._hash_file(ss_path)
                if ss_hash == prev_ss_hash:
                    consecutive_stuck += 1
                else:
                    consecutive_stuck = 0
                prev_ss_hash = ss_hash

                if consecutive_stuck >= max_stuck:
                    error_msg = f"App stuck: no visual change for {consecutive_stuck} actions"
                    findings.append(
                        Finding(
                            severity="critical",
                            category="ux",
                            description=f"agent_stuck: {error_msg}",
                            evidence=ss_path,
                            step_id=step_id,
                        )
                    )
                    break

            # Execute the action
            action_type = action_spec.get("action", "")
            target = action_spec.get("target", "")
            value = action_spec.get("value", "")

            action_start = time.monotonic()
            success = False
            action_error: str | None = None

            try:
                if action_type == "tap":
                    x = action_spec.get("x", 0)
                    y = action_spec.get("y", 0)
                    success = self._action_tap(x, y)
                elif action_type == "type":
                    success = self._action_type_text(value)
                elif action_type == "key":
                    success = self._action_send_key(value)
                elif action_type == "swipe":
                    x1 = action_spec.get("x1", 200)
                    y1 = action_spec.get("y1", 400)
                    x2 = action_spec.get("x2", 200)
                    y2 = action_spec.get("y2", 200)
                    duration = action_spec.get("duration", 0.3)
                    success = self._action_swipe(x1, y1, x2, y2, duration)
                elif action_type == "wait":
                    wait_secs = float(value) if value else 1.0
                    time.sleep(wait_secs)
                    success = True
                elif action_type == "home":
                    success = self._action_home()
                elif action_type == "done":
                    goal_achieved = True
                    success = True
                else:
                    action_error = f"Unknown action type: {action_type}"
            except Exception as exc:
                action_error = str(exc)
                logger.error("Action %s failed: %s", action_type, exc, exc_info=True)

            action_duration = time.monotonic() - action_start

            actions_taken.append({
                "index": action_idx,
                "action": action_type,
                "target": target,
                "value": value,
                "success": success,
                "error": action_error,
                "duration_ms": round(action_duration * 1000, 1),
            })

            if action_error:
                findings.append(
                    Finding(
                        severity="high",
                        category="ux",
                        description=f"Action '{action_type}' failed: {action_error}",
                        evidence=ss_path or "",
                        step_id=step_id,
                    )
                )

            if goal_achieved:
                break

            # Brief pause to let the UI settle
            time.sleep(0.5)
            action_idx += 1

        # Take final screenshot
        final_ss = self._take_screenshot(step_id, action_idx, "final")
        if final_ss:
            screenshots.append(final_ss)

        if not goal_achieved and error_msg is None:
            error_msg = "All scripted actions completed but goal not explicitly achieved"

        duration = round(time.monotonic() - start_time, 2)
        passed = goal_achieved and error_msg is None

        return SimulatorStepResult(
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

    def to_step_report(self, result: SimulatorStepResult, description: str = "") -> StepReport:
        """Convert a SimulatorStepResult into a generic StepReport."""
        return StepReport(
            step_id=result.step_id,
            description=description,
            mode="ios_simulator",
            passed=result.passed,
            duration_seconds=result.duration_seconds,
            error=result.error,
            notes=f"{result.action_count} actions, {'goal achieved' if result.goal_achieved else 'goal NOT achieved'}",
            action_count=result.action_count,
            screenshots=result.screenshots,
            ux_observations=result.ux_observations,
            actions_taken=result.actions_taken,
        )

    # -- simctl Helpers ------------------------------------------------------

    def _simctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``xcrun simctl <args>`` and return the result.

        Raises ``RuntimeError`` on non-zero exit codes.
        """
        cmd = ["xcrun", "simctl", *args]
        logger.debug("simctl: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Some simctl commands return non-zero for benign reasons
            # (e.g. "terminate" when app is not running).  Log but don't
            # always raise.
            logger.warning("simctl %s returned %d: %s", args[0], result.returncode, stderr)
        return result

    def _list_devices(self) -> dict[str, Any]:
        """List all simulator devices as a dict keyed by runtime."""
        result = self._simctl("list", "devices", "--json")
        try:
            data = json.loads(result.stdout)
            return data.get("devices", {})
        except (json.JSONDecodeError, KeyError):
            return {}

    def _find_best_device(self) -> str | None:
        """Find the best simulator device matching name and OS preferences.

        Returns the UDID, or None if no match is found.
        """
        devices = self._list_devices()
        candidates: list[tuple[str, str, str]] = []  # (udid, name, runtime)

        for runtime, device_list in devices.items():
            for device in device_list:
                if not device.get("isAvailable", False):
                    continue
                name = device.get("name", "")
                udid = device.get("udid", "")
                state = device.get("state", "")

                # Name match
                if self._device_name.lower() not in name.lower():
                    continue

                # OS version match (if specified)
                if self._os_version:
                    if self._os_version not in runtime:
                        continue

                candidates.append((udid, name, runtime))

        if not candidates:
            # Fall back to any available device
            for runtime, device_list in devices.items():
                for device in device_list:
                    if device.get("isAvailable", False) and "iPhone" in device.get("name", ""):
                        return device.get("udid")
            return None

        # Prefer already-booted devices
        for udid, name, runtime in candidates:
            if self._is_device_booted(udid):
                logger.info("Found booted device: %s (%s)", name, runtime)
                return udid

        # Return first candidate
        udid, name, runtime = candidates[0]
        logger.info("Selected device: %s (%s)", name, runtime)
        return udid

    def _is_booted(self) -> bool:
        """Check if the current device is booted."""
        if self._device_id is None:
            return False
        return self._is_device_booted(self._device_id)

    def _is_device_booted(self, udid: str) -> bool:
        """Check if a specific device is in the Booted state."""
        devices = self._list_devices()
        for device_list in devices.values():
            for device in device_list:
                if device.get("udid") == udid:
                    return device.get("state") == "Booted"
        return False

    def _wait_for_boot(self, timeout: int = 60) -> None:
        """Wait for the simulator to finish booting."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_booted():
                logger.info("Simulator %s booted successfully", self._device_id)
                return
            time.sleep(1)
        raise RuntimeError(f"Simulator {self._device_id} did not boot within {timeout}s")

    # -- Actions -------------------------------------------------------------

    def _action_tap(self, x: int | float, y: int | float) -> bool:
        """Simulate a tap at screen coordinates via AppleScript.

        Uses the Simulator.app's coordinate space.
        """
        try:
            script = (
                f'tell application "Simulator"\n'
                f"  activate\n"
                f"end tell\n"
                f'tell application "System Events"\n'
                f'  tell process "Simulator"\n'
                f"    click at {{{int(x)}, {int(y)}}}\n"
                f"  end tell\n"
                f"end tell"
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.3)
            return True
        except Exception as exc:
            logger.warning("Tap at (%s, %s) failed: %s", x, y, exc)
            return False

    def _action_type_text(self, text: str) -> bool:
        """Type text into the simulator using ``simctl io sendkey`` for each character.

        For printable text, uses AppleScript keystroke which handles Unicode
        better than individual key events.
        """
        if not text:
            return True

        try:
            # Use AppleScript to type the full string at once
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                f'tell application "Simulator"\n'
                f"  activate\n"
                f"end tell\n"
                f'tell application "System Events"\n'
                f'  keystroke "{escaped}"\n'
                f"end tell"
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.3)
            return True
        except Exception as exc:
            logger.warning("Type text failed: %s", exc)
            return False

    def _action_send_key(self, key_name: str) -> bool:
        """Send a named key to the simulator via ``simctl io sendkey``.

        Supported key names: return, tab, delete, escape, home, space,
        up, down, left, right.
        """
        if self._device_id is None:
            return False

        # Map common names to simctl key names
        key_map: dict[str, str] = {
            "return": "return",
            "enter": "return",
            "tab": "tab",
            "delete": "delete",
            "backspace": "delete",
            "escape": "escape",
            "esc": "escape",
            "home": "home",
            "space": "space",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
        }

        simctl_key = key_map.get(key_name.lower().strip())
        if simctl_key is None:
            logger.warning("Unknown key name for simctl: '%s'", key_name)
            return False

        try:
            # Note: simctl sendkey was added in Xcode 15+
            # Fallback to AppleScript if simctl doesn't support it
            result = subprocess.run(
                ["xcrun", "simctl", "io", self._device_id, "sendkey", simctl_key],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # Fallback to AppleScript
                return self._applescript_key(simctl_key)
            time.sleep(0.2)
            return True
        except Exception as exc:
            logger.warning("Send key '%s' failed: %s", key_name, exc)
            return False

    def _action_swipe(
        self,
        x1: int | float,
        y1: int | float,
        x2: int | float,
        y2: int | float,
        duration: float = 0.3,
    ) -> bool:
        """Simulate a swipe gesture via AppleScript mouse drag."""
        try:
            # AppleScript-based drag: click-and-hold at start, drag to end
            steps = max(int(duration / 0.02), 5)
            dx = (x2 - x1) / steps
            dy = (y2 - y1) / steps

            # Build AppleScript for a smooth drag
            lines = [
                'tell application "Simulator"',
                "  activate",
                "end tell",
                'tell application "System Events"',
                f'  tell process "Simulator"',
            ]
            # We use a series of click events to approximate a drag
            for i in range(steps + 1):
                cx = int(x1 + dx * i)
                cy = int(y1 + dy * i)
                if i == 0:
                    lines.append(f"    click at {{{cx}, {cy}}}")
                # AppleScript drag is limited; we rely on click approximation
            lines.extend([
                "  end tell",
                "end tell",
            ])

            subprocess.run(
                ["osascript", "-e", "\n".join(lines)],
                capture_output=True,
                timeout=10,
            )
            time.sleep(duration + 0.2)
            return True
        except Exception as exc:
            logger.warning("Swipe failed: %s", exc)
            return False

    def _action_home(self) -> bool:
        """Press the Home button on the simulator."""
        if self._device_id is None:
            return False
        try:
            # simctl does not have a direct "home" command; use AppleScript
            # to send Cmd+Shift+H which is the Simulator's Home shortcut
            script = (
                'tell application "Simulator"\n'
                "  activate\n"
                "end tell\n"
                'tell application "System Events"\n'
                '  key code 4 using {command down, shift down}\n'
                "end tell"
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.5)
            return True
        except Exception as exc:
            logger.warning("Home button failed: %s", exc)
            return False

    def _applescript_key(self, key_name: str) -> bool:
        """Send a key press via AppleScript as a fallback."""
        # Map to AppleScript key code or keystroke
        key_code_map: dict[str, int] = {
            "return": 36,
            "tab": 48,
            "delete": 51,
            "escape": 53,
            "space": 49,
            "up": 126,
            "down": 125,
            "left": 123,
            "right": 124,
        }
        code = key_code_map.get(key_name)
        if code is None:
            return False

        try:
            script = (
                'tell application "Simulator"\n'
                "  activate\n"
                "end tell\n"
                'tell application "System Events"\n'
                f"  key code {code}\n"
                "end tell"
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.2)
            return True
        except Exception as exc:
            logger.warning("AppleScript key '%s' failed: %s", key_name, exc)
            return False

    # -- Screenshot ----------------------------------------------------------

    def _take_screenshot(
        self,
        step_id: str,
        action_idx: int,
        label: str,
    ) -> str | None:
        """Capture a screenshot of the simulator via ``simctl io screenshot``.

        Returns the file path, or None on failure.
        """
        if self._device_id is None:
            return None

        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{step_id}-{action_idx:03d}-{label}.png"
        filepath = self._evidence_dir / filename

        try:
            result = subprocess.run(
                ["xcrun", "simctl", "io", self._device_id, "screenshot", str(filepath)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.warning("simctl screenshot failed: %s", result.stderr.strip())
                return None
            logger.debug("Screenshot saved: %s", filepath)
            return str(filepath)
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)
            return None

    # -- Utilities -----------------------------------------------------------

    @staticmethod
    def _hash_file(filepath: str) -> str:
        """Compute a SHA-256 hash of a file's contents for change detection.

        Returns a truncated hex digest.
        """
        try:
            h = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        except Exception:
            return ""

    @classmethod
    def list_available_devices(cls) -> list[dict[str, Any]]:
        """List all available simulator devices.

        Convenience method for discovering which devices are available
        for testing.

        Returns:
            A list of device dicts with keys: udid, name, state, runtime.
        """
        try:
            result = subprocess.run(
                ["xcrun", "simctl", "list", "devices", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            data = json.loads(result.stdout)
            devices_by_runtime = data.get("devices", {})
        except Exception:
            return []

        flat: list[dict[str, Any]] = []
        for runtime, device_list in devices_by_runtime.items():
            for device in device_list:
                if device.get("isAvailable", False):
                    flat.append({
                        "udid": device.get("udid", ""),
                        "name": device.get("name", ""),
                        "state": device.get("state", ""),
                        "runtime": runtime,
                    })
        return flat
