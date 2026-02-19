"""GhostQA Browser Runner — Playwright browser orchestration.

Launches headless Chromium, sets viewport, injects auth cookies, navigates to
URLs, and runs an AI persona agent loop: screenshot -> decide -> act -> repeat.

Includes stuck detection (screenshot hash comparison, DOM-level change tracking,
repetition detection), automatic overlay dismissal, and checkpoint validation.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
import time
from pathlib import Path
from typing import Any

from ghostqa.engine.action_executor import ActionExecutor, PersonaDecision
from ghostqa.engine.cost_tracker import BudgetExceededError, CostTracker
from ghostqa.engine.persona_agent import AgentStuckError, PersonaAgent
from ghostqa.engine.report_generator import Finding, StepReport

logger = logging.getLogger("ghostqa.engine.browser_runner")


@dataclasses.dataclass
class BrowserStepResult:
    """Result of executing a single browser step."""

    step_id: str
    passed: bool
    screenshots: list[str]
    ux_observations: list[str]
    actions_taken: list[dict[str, Any]]
    action_count: int
    duration_seconds: float
    checkpoints_reached: list[str]
    findings: list[Finding]
    error: str | None = None
    goal_achieved: bool = False


class BrowserRunner:
    """Executes browser steps from scenario definitions.

    Manages a Playwright browser context, injects cookies, takes screenshots,
    and runs the persona agent loop (screenshot -> AI decision -> action ->
    next screenshot) until the goal is achieved, max actions reached, or the
    agent gets stuck.
    """

    def __init__(
        self,
        frontend_url: str,
        product_config: dict[str, Any],
        persona: dict[str, Any],
        cost_tracker: CostTracker,
        evidence_dir: Path,
        api_cookies: dict[str, str] | None = None,
        api_key: str | None = None,
    ) -> None:
        """
        Args:
            frontend_url: Base URL for the frontend (e.g. "http://localhost:3000").
            product_config: Product configuration dict.
            persona: Persona configuration dict.
            cost_tracker: Shared cost tracker instance.
            evidence_dir: Directory to save screenshots and evidence.
            api_cookies: Cookies captured from API steps to inject into browser.
            api_key: Anthropic API key to pass to PersonaAgent.
        """
        self._frontend_url = frontend_url.rstrip("/")
        self._product_config = product_config
        self._persona = persona
        self._cost_tracker = cost_tracker
        self._evidence_dir = evidence_dir
        self._api_cookies = api_cookies or {}
        self._api_key = api_key
        self._viewports = product_config.get("viewports", {})

        # Managed browser lifecycle -- set by start()/stop()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cookies_injected: bool = False

    # -- Browser Lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Launch the Playwright browser. Call once before execute_step()."""
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)

    def stop(self) -> None:
        """Close the browser and Playwright. Call once after all steps complete."""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._cookies_injected = False

    def _ensure_context(
        self,
        viewport_size: tuple[int, int],
        device_scale_factor: int,
        inject_cookies: list[dict[str, Any]],
        captured_vars: dict[str, Any],
    ) -> Any:
        """Return the existing page, or create context + page on first call.

        The context (and therefore cookies / localStorage / sessionStorage)
        persists across all browser steps in the same scenario run.
        """
        if self._page is not None:
            return self._page

        self._context = self._browser.new_context(
            viewport={"width": viewport_size[0], "height": viewport_size[1]},
            device_scale_factor=device_scale_factor,
        )

        # Inject cookies once when context is first created
        cookies_to_add = self._prepare_cookies(inject_cookies, captured_vars)
        if cookies_to_add:
            self._context.add_cookies(cookies_to_add)
        self._cookies_injected = True

        self._page = self._context.new_page()
        return self._page

    def execute_step(
        self,
        step: dict[str, Any],
        captured_vars: dict[str, Any],
    ) -> BrowserStepResult:
        """Execute a single browser step using Playwright + AI persona agent.

        The browser and context are managed externally via start()/stop().
        Cookies, localStorage, sessionStorage, and service workers persist
        across consecutive browser steps within the same scenario run.

        Args:
            step: Browser step definition from the scenario YAML.
            captured_vars: Variables captured from previous steps.

        Returns:
            BrowserStepResult with screenshots, UX observations, pass/fail.
        """
        step_id = step.get("id", "unknown")
        goal = step.get("goal", "")
        max_actions = step.get("max_actions", 40)
        max_duration = step.get("max_duration_seconds", 300)
        viewport_name = step.get("viewport", "desktop")
        start_url = step.get("start_url")  # None if not explicitly set
        checkpoints = step.get("checkpoints", [])
        inject_cookies = step.get("inject_cookies", [])
        use_local_model = step.get("use_local_model", True)

        # Resolve viewport
        viewport = self._resolve_viewport(viewport_name)
        viewport_size = (viewport.get("width", 1440), viewport.get("height", 900))

        logger.info(
            "Browser step %s: viewport=%s (%dx%d), max_actions=%d, goal=%s",
            step_id, viewport_name, viewport_size[0], viewport_size[1],
            max_actions, goal[:80],
        )

        screenshots: list[str] = []
        ux_observations: list[str] = []
        actions_taken: list[dict[str, Any]] = []
        checkpoints_reached: list[str] = []
        findings: list[Finding] = []
        goal_achieved = False
        error_msg: str | None = None

        start_time = time.monotonic()

        # Reuse the managed browser -- context and page persist across steps
        page = self._ensure_context(
            viewport_size=viewport_size,
            device_scale_factor=viewport.get("device_scale_factor", 1),
            inject_cookies=inject_cookies,
            captured_vars=captured_vars,
        )

        # Navigate to start URL (if explicitly set), or wait for current page to settle
        if start_url is not None:
            full_url = self._frontend_url + start_url
            try:
                page.goto(full_url, wait_until="domcontentloaded", timeout=30_000)
                # Wait for the page to be fully interactive (React/Next.js hydration).
                # networkidle gives us confidence that deferred JS bundles have loaded;
                # the extra 1 s pause lets framework event-handlers finish attaching.
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass  # Some pages don't reach networkidle -- that's fine
                page.wait_for_timeout(1000)
            except Exception as exc:
                error_msg = f"Failed to navigate to {full_url}: {exc}"
                logger.error(error_msg)
                findings.append(Finding(
                    severity="block",
                    category="server_error",
                    description=error_msg,
                    evidence="",
                    step_id=step_id,
                ))
                return BrowserStepResult(
                    step_id=step_id,
                    passed=False,
                    screenshots=screenshots,
                    ux_observations=ux_observations,
                    actions_taken=actions_taken,
                    action_count=0,
                    duration_seconds=round(time.monotonic() - start_time, 2),
                    checkpoints_reached=checkpoints_reached,
                    findings=findings,
                    error=error_msg,
                    goal_achieved=False,
                )
        else:
            # No explicit start_url -- stay on current page, wait for it to settle
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            page.wait_for_timeout(500)

        # Initialize persona agent and action executor
        agent = PersonaAgent(
            persona=self._persona,
            viewport_name=viewport_name,
            viewport_size=viewport_size,
            cost_tracker=self._cost_tracker,
            api_key=self._api_key,
        )
        executor = ActionExecutor(page, device_scale_factor=viewport.get("device_scale_factor", 1))

        # Agent loop: screenshot -> decide -> act -> repeat
        # Stuck detection -- fail fast on dead ends
        _recent_screenshot_hashes: list[int] = []  # hash of last N screenshot bytes
        _recent_actions: list[tuple[str, str]] = []  # (action, target) for repetition detection
        _consecutive_no_progress = 0  # actions without visible page change (screenshot-based)
        _consecutive_no_change = 0  # actions where page_changed=False (DOM-based, faster)
        _consecutive_local_failures = 0  # consecutive failed local model actions
        _local_model_disabled = True  # Always use API for browser steps -- local models can't interpret web UIs reliably
        MAX_NO_PROGRESS = 5  # force API escalation after this many
        MAX_NO_CHANGE = 5  # abort after this many DOM-verified no-change actions
        MAX_REPEATS = 3  # same action+target repeated this many times = stuck

        action_idx = 0
        while action_idx < max_actions:
            elapsed = time.monotonic() - start_time
            if elapsed > max_duration:
                error_msg = (
                    f"Browser step timed out after {elapsed:.0f}s "
                    f"(limit: {max_duration}s)"
                )
                findings.append(Finding(
                    severity="high",
                    category="performance",
                    description=error_msg,
                    evidence="",
                    step_id=step_id,
                ))
                break

            # 1. Take screenshot
            screenshot_b64, screenshot_path = self._take_screenshot(
                page, step_id, action_idx, "before"
            )
            screenshots.append(screenshot_path)

            # Track screenshot for stuck detection
            screenshot_hash = hash(screenshot_b64) if screenshot_b64 else 0
            if _recent_screenshot_hashes and screenshot_hash == _recent_screenshot_hashes[-1]:
                _consecutive_no_progress += 1
            else:
                _consecutive_no_progress = 0
            _recent_screenshot_hashes.append(screenshot_hash)
            if len(_recent_screenshot_hashes) > 10:
                _recent_screenshot_hashes.pop(0)

            # Stuck detection: if no progress for MAX_NO_PROGRESS actions, force API
            force_api = _local_model_disabled  # Persist local model disable across actions
            stuck_context = None
            if _consecutive_no_progress >= MAX_NO_PROGRESS or _consecutive_no_change >= 3:
                force_api = True
                no_progress_count = max(_consecutive_no_progress, _consecutive_no_change)
                stuck_context = (
                    "WARNING: The page has not changed for the last "
                    f"{no_progress_count} actions. You are stuck in a loop. "
                    "Try a COMPLETELY DIFFERENT approach. If you've been clicking "
                    "something that doesn't work, try a different element. "
                    "If you've filled a form, look for a Submit/Next/Continue button. "
                    "If nothing works, report 'stuck' so we can move on."
                )
                logger.warning(
                    "Browser step %s: no progress for %d actions "
                    "(screenshot=%d, DOM=%d), forcing API escalation",
                    step_id, no_progress_count,
                    _consecutive_no_progress, _consecutive_no_change,
                )

            # Repetition detection: same action+target 3x = stuck
            if not force_api and len(_recent_actions) >= MAX_REPEATS:
                last_n = _recent_actions[-MAX_REPEATS:]
                if len(set(last_n)) == 1:
                    force_api = True
                    stuck_context = (
                        f"WARNING: You have repeated the exact same action "
                        f"'{last_n[0][0]}' on '{last_n[0][1]}' {MAX_REPEATS} times. "
                        "This is not working. Try something COMPLETELY DIFFERENT. "
                        "Look at the screen carefully and find an alternative path."
                    )
                    logger.warning(
                        "Browser step %s: action repeated %dx, forcing API escalation",
                        step_id, MAX_REPEATS,
                    )

            # Hard stuck: if no progress for 2x the limit, give up on this step
            if _consecutive_no_progress >= MAX_NO_PROGRESS * 2:
                error_msg = (
                    f"Agent stuck: no page change for {_consecutive_no_progress} "
                    f"consecutive actions"
                )
                findings.append(Finding(
                    severity="critical",
                    category="ux",
                    description=f"agent_stuck: {error_msg}",
                    evidence=screenshot_path,
                    step_id=step_id,
                ))
                logger.error("Browser step %s: hard stuck, aborting step", step_id)
                break

            # 2. Send to persona agent for decision
            try:
                decision = agent.decide(
                    goal=goal,
                    screenshot_base64=screenshot_b64,
                    checkpoints=checkpoints,
                    use_local_model=use_local_model,
                    force_api=force_api,
                    stuck_context=stuck_context,
                )
            except AgentStuckError as exc:
                error_msg = str(exc)
                findings.append(Finding(
                    severity="critical",
                    category="ux",
                    description=f"agent_stuck: {error_msg}",
                    evidence=screenshot_path,
                    step_id=step_id,
                ))
                break
            except BudgetExceededError as exc:
                error_msg = str(exc)
                findings.append(Finding(
                    severity="block",
                    category="performance",
                    description=f"Budget exceeded: {error_msg}",
                    evidence="",
                    step_id=step_id,
                ))
                break

            # Record UX observations
            if decision.ux_notes:
                ux_observations.append(decision.ux_notes)
                findings.append(Finding(
                    severity="medium",
                    category="ux",
                    description=f"ux_confusion: {decision.ux_notes}",
                    evidence=screenshot_path,
                    step_id=step_id,
                ))

            # Record checkpoint
            if decision.checkpoint and decision.checkpoint not in checkpoints_reached:
                checkpoints_reached.append(decision.checkpoint)
                logger.info(
                    "Browser step %s: checkpoint reached: %s",
                    step_id, decision.checkpoint,
                )
                # Take a checkpoint screenshot
                _, cp_path = self._take_screenshot(
                    page, step_id, action_idx, f"checkpoint-{decision.checkpoint}"
                )
                screenshots.append(cp_path)

                # Validate checkpoint assertions
                self._validate_checkpoint(
                    page, decision.checkpoint, checkpoints,
                    step_id, findings, cp_path,
                )

            # Check if goal achieved
            if decision.goal_achieved or decision.action == "done":
                goal_achieved = True
                actions_taken.append({
                    "index": action_idx,
                    "action": decision.action,
                    "target": decision.target,
                    "observation": decision.observation,
                    "reasoning": decision.reasoning,
                })
                # Take final screenshot
                _, final_path = self._take_screenshot(
                    page, step_id, action_idx, "goal-achieved"
                )
                screenshots.append(final_path)
                break

            # Check if stuck (but not yet at MAX_CONSECUTIVE_STUCK)
            if decision.action == "stuck":
                actions_taken.append({
                    "index": action_idx,
                    "action": "stuck",
                    "target": "",
                    "observation": decision.observation,
                    "reasoning": decision.reasoning,
                })
                action_idx += 1
                continue

            # 3. Execute the action
            action_result = executor.execute(decision)
            action_record = {
                "index": action_idx,
                "action": decision.action,
                "target": decision.target,
                "value": decision.value,
                "observation": decision.observation,
                "reasoning": decision.reasoning,
                "success": action_result.success,
                "error": action_result.error,
                "duration_ms": action_result.duration_ms,
                "page_changed": action_result.page_changed,
                "change_details": action_result.change_details,
            }
            if action_result.sidebar_auto_dismissals > 0:
                action_record["sidebar_auto_dismissals"] = action_result.sidebar_auto_dismissals
            actions_taken.append(action_record)

            # Track DOM-level page change for faster stuck detection.
            # page_changed=False means the action succeeded but had no
            # observable effect on the page DOM -- more precise than
            # screenshot hash comparison.
            if not action_result.page_changed:
                _consecutive_no_change += 1
            else:
                _consecutive_no_change = 0

            if _consecutive_no_change >= MAX_NO_CHANGE:
                error_msg = (
                    f"Agent stuck: {_consecutive_no_change} consecutive "
                    f"actions with no page effect (DOM-verified)"
                )
                findings.append(Finding(
                    severity="critical",
                    category="ux",
                    description=f"agent_stuck: {error_msg}",
                    evidence=screenshot_path,
                    step_id=step_id,
                ))
                logger.error(
                    "Browser step %s: %d actions with no DOM change, aborting",
                    step_id, _consecutive_no_change,
                )
                break

            if not action_result.success:
                logger.warning(
                    "Browser step %s action %d failed: %s",
                    step_id, action_idx, action_result.error,
                )
                findings.append(Finding(
                    severity="high",
                    category="ux",
                    description=f"element_not_found: Action '{decision.action}' "
                                f"on '{decision.target}' failed: {action_result.error}",
                    evidence=screenshot_path,
                    step_id=step_id,
                ))

            # Track consecutive local model failures.
            # If force_api was False and use_local_model was True, the local
            # model path was eligible. A failed action in that context counts
            # as a local model failure.
            _used_local_path = (not force_api) and use_local_model
            if _used_local_path and not action_result.success:
                _consecutive_local_failures += 1
                if _consecutive_local_failures >= 2 and not _local_model_disabled:
                    _local_model_disabled = True
                    logger.warning(
                        "Browser step %s: local model failed %d+ times "
                        "consecutively, forcing API for remainder of step",
                        step_id, _consecutive_local_failures,
                    )
            elif action_result.success:
                # Only reset on success -- don't reset on API-path failures
                if not _local_model_disabled:
                    _consecutive_local_failures = 0

            # Track action for repetition detection
            _recent_actions.append((decision.action, decision.target[:50]))
            if len(_recent_actions) > 10:
                _recent_actions.pop(0)

            # 4. Wait for page to settle
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass  # Not all pages reach networkidle; that's okay

            action_idx += 1

        # End of loop -- check if we ran out of actions
        if not goal_achieved and error_msg is None:
            error_msg = f"Max actions ({max_actions}) reached without achieving goal"
            findings.append(Finding(
                severity="critical",
                category="behavior",
                description=f"goal_not_achieved: {error_msg}",
                evidence=screenshots[-1] if screenshots else "",
                step_id=step_id,
            ))

        duration = round(time.monotonic() - start_time, 2)
        passed = goal_achieved and error_msg is None

        return BrowserStepResult(
            step_id=step_id,
            passed=passed,
            screenshots=screenshots,
            ux_observations=ux_observations,
            actions_taken=actions_taken,
            action_count=action_idx,
            duration_seconds=duration,
            checkpoints_reached=checkpoints_reached,
            findings=findings,
            error=error_msg,
            goal_achieved=goal_achieved,
        )

    def to_step_report(self, result: BrowserStepResult, description: str = "") -> StepReport:
        """Convert a BrowserStepResult into a generic StepReport."""
        # Get model routing breakdown from cost tracker
        cost_summary = self._cost_tracker.get_summary()
        model_routing = cost_summary.calls_by_model

        return StepReport(
            step_id=result.step_id,
            description=description,
            mode="browser",
            passed=result.passed,
            duration_seconds=result.duration_seconds,
            error=result.error,
            notes=f"{result.action_count} actions, "
                  f"{'goal achieved' if result.goal_achieved else 'goal NOT achieved'}",
            action_count=result.action_count,
            screenshots=result.screenshots,
            ux_observations=result.ux_observations,
            actions_taken=result.actions_taken,
            model_routing=model_routing,
        )

    def _take_screenshot(
        self,
        page: Any,
        step_id: str,
        action_idx: int,
        label: str,
    ) -> tuple[str, str]:
        """Take a screenshot and save it to the evidence directory.

        Returns (base64_data, file_path).
        """
        filename = f"{step_id}-{action_idx:03d}-{label}.png"
        filepath = self._evidence_dir / filename

        # Ensure directory exists
        self._evidence_dir.mkdir(parents=True, exist_ok=True)

        try:
            screenshot_bytes = page.screenshot(full_page=False)
            filepath.write_bytes(screenshot_bytes)
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            return b64, str(filepath)
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)
            return "", str(filepath)

    def _resolve_viewport(self, viewport_name: str) -> dict[str, Any]:
        """Resolve a viewport name to its configuration."""
        if viewport_name in self._viewports:
            return self._viewports[viewport_name]
        # Fallback to desktop
        logger.warning("Unknown viewport '%s', falling back to desktop", viewport_name)
        return self._viewports.get("desktop", {"width": 1440, "height": 900})

    def _prepare_cookies(
        self,
        inject_cookies: list[dict[str, Any]],
        captured_vars: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Prepare cookies for injection into the browser context.

        Parses Set-Cookie header values and also includes cookies from the
        requests session.
        """
        cookies: list[dict[str, Any]] = []

        # Inject from captured variables (Set-Cookie headers from API steps)
        for spec in inject_cookies:
            from_capture = spec.get("from_capture", "")
            raw_cookie = captured_vars.get(from_capture, "")
            if raw_cookie:
                parsed = self._parse_set_cookie(raw_cookie)
                if parsed:
                    cookies.append(parsed)

        # Also inject any cookies from the API runner's session
        for name, value in self._api_cookies.items():
            cookies.append({
                "name": name,
                "value": value,
                "url": self._frontend_url,
            })

        # Filter out any cookies with empty name or value -- Playwright rejects them
        return [c for c in cookies if c.get("name") and c.get("value")]

    def _parse_set_cookie(self, raw: str) -> dict[str, Any] | None:
        """Parse a Set-Cookie header into a Playwright cookie dict.

        Playwright's ``add_cookies`` accepts EITHER ``url`` (from which it
        infers domain and path) OR explicit ``domain`` + ``path`` -- but not
        both.  We always set ``url`` and let Playwright infer the rest, so we
        intentionally skip ``path=`` and ``domain=`` attributes from the raw
        Set-Cookie header.
        """
        if not raw:
            return None
        # Set-Cookie: name=value; Path=/; HttpOnly; ...
        parts = raw.split(";")
        if not parts:
            return None
        name_value = parts[0].strip()
        if "=" not in name_value:
            return None
        name, value = name_value.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            return None
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "url": self._frontend_url,
        }
        # Parse optional flags -- skip path/domain so Playwright infers from url
        for part in parts[1:]:
            part = part.strip().lower()
            if part == "secure":
                cookie["secure"] = True
            elif part == "httponly":
                cookie["httpOnly"] = True
        return cookie

    def _validate_checkpoint(
        self,
        page: Any,
        checkpoint_name: str,
        checkpoints: list[dict[str, Any]],
        step_id: str,
        findings: list[Finding],
        screenshot_path: str,
    ) -> None:
        """Validate checkpoint assertions (assert_text, performance, etc.)."""
        for cp in checkpoints:
            if cp.get("after") != checkpoint_name:
                continue

            # Assert text exists on page
            assert_text = cp.get("assert_text")
            if assert_text:
                try:
                    locator = page.get_by_text(assert_text, exact=False)
                    if locator.count() == 0:
                        findings.append(Finding(
                            severity="high",
                            category="behavior",
                            description=(
                                f"Checkpoint '{checkpoint_name}' assertion failed: "
                                f"text '{assert_text}' not found on page"
                            ),
                            evidence=screenshot_path,
                            step_id=step_id,
                        ))
                except Exception as exc:
                    findings.append(Finding(
                        severity="high",
                        category="behavior",
                        description=(
                            f"Checkpoint '{checkpoint_name}' assertion error: {exc}"
                        ),
                        evidence=screenshot_path,
                        step_id=step_id,
                    ))

            # Performance assertions
            perf = cp.get("performance", {})
            max_ms = perf.get("max_ms")
            if max_ms is not None:
                # Try to get page load timing via Performance API
                try:
                    timing = page.evaluate(
                        "() => { const e = performance.getEntriesByType('navigation')[0]; "
                        "return e ? e.loadEventEnd - e.startTime : null; }"
                    )
                    if timing is not None and timing > max_ms:
                        findings.append(Finding(
                            severity="high",
                            category="performance",
                            description=(
                                f"performance exceeded: Page load {timing:.0f}ms > "
                                f"{max_ms}ms target at checkpoint '{checkpoint_name}'"
                            ),
                            evidence=screenshot_path,
                            step_id=step_id,
                        ))
                except Exception:
                    pass  # Performance API may not be available
