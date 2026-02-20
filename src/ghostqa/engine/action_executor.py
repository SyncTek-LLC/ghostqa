"""GhostQA Action Executor — Translates AI persona decisions into Playwright commands.

Maps high-level action types (click, fill, navigate, scroll, keyboard, wait,
done, stuck) to concrete Playwright page interactions.  Includes modal-aware
element scoping, overlay auto-dismiss, cookie injection, and post-action
page-change verification.

This module is the bridge between the persona agent's high-level decisions
and the actual browser automation layer (Playwright).
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger("ghostqa.engine.action_executor")


@dataclasses.dataclass
class PersonaDecision:
    """A decision made by the persona agent after viewing a screenshot."""

    observation: str
    action: str  # click, fill, navigate, scroll, keyboard, wait, done, stuck
    target: str  # Description or coordinates
    value: str  # Text to type (for fill), URL (for navigate), key (for keyboard)
    reasoning: str
    ux_notes: str | None
    checkpoint: str | None
    goal_achieved: bool

    @staticmethod
    def _coerce_str(val: Any) -> str:
        """Coerce any value to a string.

        - If val is a list: join with ", "
        - If val is None: return ""
        - Otherwise: return str(val)
        """
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        if val is None:
            return ""
        return str(val)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaDecision:
        return cls(
            observation=cls._coerce_str(data.get("observation", "")),
            action=cls._coerce_str(data.get("action", "stuck")),
            target=cls._coerce_str(data.get("target", "")),
            value=cls._coerce_str(data.get("value", "")),
            reasoning=cls._coerce_str(data.get("reasoning", "")),
            ux_notes=cls._coerce_str(data.get("ux_notes")) or None,
            checkpoint=data.get("checkpoint"),
            goal_achieved=data.get("goal_achieved", False),
        )


@dataclasses.dataclass
class ActionResult:
    """Result of executing a single action against the browser."""

    success: bool
    action: str
    target: str
    error: str | None = None
    duration_ms: float = 0.0
    page_changed: bool = True  # Whether the action caused a detectable page change
    change_details: list[str] = dataclasses.field(default_factory=list)  # What changed
    sidebar_auto_dismissals: int = 0  # How many times an overlay was auto-dismissed


class ActionExecutor:
    """Translates PersonaDecision objects into Playwright page interactions."""

    # Timeout for post-action page settling (ms)
    SETTLE_TIMEOUT_MS = 10_000

    # CSS selector metacharacters that must be escaped
    _CSS_META = str.maketrans(
        {
            '"': r"\"",
            "'": r"\'",
            "[": r"\[",
            "]": r"\]",
            "\\": "\\\\",
            "{": r"\{",
            "}": r"\}",
        }
    )

    def __init__(self, page: Page, device_scale_factor: int = 1) -> None:
        self._page = page
        self._dpr = device_scale_factor
        # Cache CSS viewport dimensions for coordinate space detection
        vp = page.viewport_size or {"width": 1440, "height": 900}
        self._css_width: int = vp["width"]
        self._css_height: int = vp["height"]

    # -- Modal-Aware Scoping -------------------------------------------------

    def _get_active_scope(self):
        """Return the most specific interactive scope -- a visible modal dialog if one exists, else the page.

        When a modal dialog is open, DOM elements behind it are blocked by the
        backdrop overlay.  Scoping element searches to the modal prevents finding
        elements that can't actually receive pointer events (e.g. a button with
        the same name that sits behind the modal).

        Checks for aria-modal dialogs first (strongest signal), then any
        visible [role="dialog"].
        """
        try:
            modal = self._page.locator('[role="dialog"][aria-modal="true"]:visible')
            if modal.count() > 0:
                return modal.first
            # Also check for non-aria modals (some frameworks omit aria-modal)
            modal = self._page.locator('[role="dialog"]:visible')
            if modal.count() > 0:
                return modal.first
        except Exception:
            pass
        return self._page

    # -- Overlay / Sidebar Auto-Dismiss --------------------------------------

    def _dismiss_overlay(self) -> bool:
        """Attempt to dismiss an overlay that is intercepting pointer events.

        Tries three strategies in order:
        1. Click a close button via common aria-labels
        2. Press the Escape key (most overlays respond to Escape)
        3. Click coordinates outside typical sidebar bounds (backdrop tap)

        Returns True if any dismissal strategy executed without error,
        False if all strategies failed.
        """
        # Strategy 1: Try close buttons with common aria-labels
        close_labels = [
            "Close navigation menu",
            "Close navigation",
            "Close menu",
            "Close sidebar",
            "Close",
            "Dismiss",
            "Close dialog",
            "Close modal",
        ]
        for label in close_labels:
            try:
                btn = self._page.locator(f'[aria-label="{label}"]')
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=3000)
                    logger.info(
                        "Overlay dismissed via close button: aria-label='%s'",
                        label,
                    )
                    return True
            except Exception:
                continue

        # Strategy 2: Press Escape key
        try:
            self._page.keyboard.press("Escape")
            # Check if pressing Escape actually changed something by waiting
            # briefly and seeing if the page re-renders
            self._page.wait_for_timeout(300)
            logger.info("Overlay dismissed via Escape key")
            return True
        except Exception:
            pass

        # Strategy 3: Click the backdrop area outside a typical sidebar.
        # Mobile sidebars are usually ~288px wide (w-72 in Tailwind).  On a
        # 390px-wide viewport, clicking at x=350 hits the backdrop overlay
        # to the right of the sidebar.  Use force=True to bypass any
        # pointer-event checks on the backdrop element itself.
        try:
            self._page.mouse.click(
                min(self._css_width - 40, 350),  # right side of viewport
                self._css_height // 2,  # vertical center
            )
            logger.info("Overlay dismissed via backdrop click")
            return True
        except Exception:
            pass

        return False

    def _is_overlay_interception_error(self, error: Exception) -> bool:
        """Check whether an exception indicates an overlay/sidebar intercepted a click.

        Matches Playwright error messages like:
        - '<a href="/ops/schedule"> from <aside id="mobile-sidebar"> subtree intercepts pointer events'
        - 'locator.click: Element is not visible' (when overlay covers the target)
        - Any error mentioning 'intercepts pointer events'
        """
        msg = str(error).lower()
        return (
            "intercepts pointer events" in msg or "mobile-sidebar" in msg or ("sidebar" in msg and "intercept" in msg)
        )

    # -- Post-Action Verification --------------------------------------------

    def _capture_page_state(self) -> dict:
        """Capture a lightweight fingerprint of the current page state.

        Used to detect whether an action actually changed anything.
        Returns a dict with: url, title, modal_count, visible_text_hash,
        focused_element, scroll_y, form_count, alert_count.
        """
        try:
            state = self._page.evaluate("""() => {
                const modals = document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], .modal, [data-modal], [aria-modal="true"]'
                );
                const visibleText = document.body?.innerText?.slice(0, 2000) || '';
                const focused = document.activeElement;
                // Detect the actual scroll container (may be a nested <main>)
                const main = document.querySelector('main[class*="overflow"]')
                    || document.querySelector('main');
                const scrollTarget = (main && main.scrollHeight > main.clientHeight)
                    ? main : null;
                return {
                    url: window.location.href,
                    title: document.title,
                    modal_count: modals.length,
                    visible_modals: Array.from(modals).filter(m => m.offsetParent !== null).length,
                    text_hash: visibleText.length,
                    scroll_y: scrollTarget ? scrollTarget.scrollTop : window.scrollY,
                    focused_tag: focused?.tagName?.toLowerCase() || null,
                    focused_id: focused?.id || null,
                    form_count: document.forms.length,
                    alert_count: document.querySelectorAll('[role="alert"], .alert, .error, .toast').length,
                };
            }""")
            return state
        except Exception:
            return {}

    def _detect_page_change(self, before: dict, after: dict) -> dict:
        """Compare two page states and report what changed.

        Returns a dict with:
          changed: bool -- whether anything meaningful changed
          changes: list[str] -- description of what changed
          navigation: bool -- whether URL changed
          modal_opened: bool -- whether a new modal appeared
          modal_closed: bool -- whether a modal disappeared
          content_changed: bool -- whether visible text changed
        """
        if not before or not after:
            return {
                "changed": True,
                "changes": ["state capture failed"],
                "navigation": False,
                "modal_opened": False,
                "modal_closed": False,
                "content_changed": False,
            }

        changes: list[str] = []

        navigation = before.get("url") != after.get("url")
        if navigation:
            changes.append(f"navigated: {before.get('url')} -> {after.get('url')}")

        modal_opened = after.get("visible_modals", 0) > before.get("visible_modals", 0)
        modal_closed = after.get("visible_modals", 0) < before.get("visible_modals", 0)
        if modal_opened:
            changes.append("modal opened")
        if modal_closed:
            changes.append("modal closed")

        content_changed = before.get("text_hash") != after.get("text_hash")
        if content_changed:
            changes.append("content changed")

        if before.get("scroll_y") != after.get("scroll_y"):
            changes.append("scrolled")

        if before.get("focused_tag") != after.get("focused_tag") or before.get("focused_id") != after.get("focused_id"):
            changes.append(f"focus moved to {after.get('focused_tag')}#{after.get('focused_id', '')}")

        if before.get("alert_count", 0) != after.get("alert_count", 0):
            changes.append("alert/error appeared or disappeared")

        return {
            "changed": len(changes) > 0,
            "changes": changes,
            "navigation": navigation,
            "modal_opened": modal_opened,
            "modal_closed": modal_closed,
            "content_changed": content_changed,
        }

    # -- Coordinate Conversion -----------------------------------------------

    def _to_css_coords(self, x: float, y: float) -> tuple[float, float]:
        """Convert AI-reported coordinates to CSS pixel coordinates.

        The AI persona sees the screenshot (which is DPR * viewport size pixels)
        but the system prompt tells it the viewport size in CSS pixels.  In
        practice, Claude vision reports coordinates in the CSS coordinate space
        (e.g. 0-390 for a 390-wide mobile viewport), NOT in the full-resolution
        screenshot space (0-1170).

        Playwright's ``mouse.click(x, y)`` expects CSS coordinates, so:
        - If the coordinates already fit within the CSS viewport, use them as-is.
        - If they exceed the CSS viewport (suggesting full-resolution pixel
          space), divide by DPR to convert.

        This heuristic handles both cases robustly -- whether the AI reports in
        CSS space (most common with Claude) or in screenshot pixel space.
        """
        if self._dpr <= 1:
            return x, y

        # Heuristic: if both x and y fit within the CSS viewport, the AI is
        # already reporting CSS coordinates.  We add a small margin (10%) to
        # account for coordinates that land slightly outside the viewport edge
        # (e.g. a partially-visible element).
        x_limit = self._css_width * 1.1
        y_limit = self._css_height * 1.1
        if x <= x_limit and y <= y_limit:
            # Coordinates are in CSS space -- use as-is
            return x, y

        # Coordinates exceed CSS viewport -- likely in full-resolution pixel space
        return x / self._dpr, y / self._dpr

    @staticmethod
    def _sanitize_for_selector(text: str) -> str:
        """Escape CSS selector metacharacters in text.

        Prevents injection when interpolating AI-generated text into
        CSS attribute selectors like ``input[name*="..."]``.
        """
        return text.translate(ActionExecutor._CSS_META)

    def execute(self, decision: PersonaDecision) -> ActionResult:
        """Execute a persona decision against the Playwright page.

        Returns ActionResult. Never raises on action failure -- captures the
        error and returns it in the result.
        """
        action = decision.action.lower().strip()

        # Normalize action type variants to standard names
        action_aliases = {
            "fill out": "fill",
            "fill in": "fill",
            "type": "fill",
            "type in": "fill",
            "enter": "fill",
            "input": "fill",
            "click on": "click",
            "tap": "click",
            "press": "click",
            "select": "fill",  # <select> dropdowns -> route through _do_fill's select detection
            "go to": "navigate",
            "goto": "navigate",
            "open": "navigate",
            "visit": "navigate",
            "scroll down": "scroll",
            "scroll up": "scroll",
        }

        # Try exact match first, then try first word for multi-word actions
        if action in action_aliases:
            action = action_aliases[action]
        else:
            first_word = action.split()[0] if action else ""
            if first_word in action_aliases:
                action = action_aliases[first_word]

        start = time.monotonic()

        # Actions that skip verification (no meaningful DOM state to compare).
        # fill/keyboard always change form state but innerText doesn't reflect
        # input values, so text_hash comparison produces false negatives.
        _skip_verify = ("done", "stuck", "wait", "scroll", "fill", "keyboard")

        # Capture page state before action for verification
        state_before = self._capture_page_state() if action not in _skip_verify else {}

        overlay_dismissals = 0

        try:
            if action == "click":
                # Wrap click execution with overlay-interception retry.
                # When an overlay (sidebar, modal backdrop, etc.) intercepts the
                # click, Playwright raises an error mentioning "intercepts pointer
                # events".  We detect this, dismiss the overlay, and retry ONCE.
                try:
                    self._do_click(decision)
                except Exception as click_exc:
                    if self._is_overlay_interception_error(click_exc):
                        logger.warning(
                            "Overlay intercepting click on '%s' -- auto-dismissing",
                            decision.target,
                        )
                        dismissed = self._dismiss_overlay()
                        if dismissed:
                            overlay_dismissals += 1
                            self._page.wait_for_timeout(500)  # allow 300ms transition + margin
                            # Retry the click once after dismissal
                            self._do_click(decision)
                        else:
                            raise  # re-raise if dismissal failed
                    else:
                        raise  # non-overlay error -- propagate normally
            elif action == "fill":
                self._do_fill(decision)
            elif action == "navigate":
                self._do_navigate(decision)
            elif action == "scroll":
                self._do_scroll(decision)
            elif action == "keyboard":
                self._do_keyboard(decision)
            elif action == "wait":
                self._do_wait(decision)
            elif action in ("done", "stuck"):
                # No browser action needed
                pass
            else:
                return ActionResult(
                    success=False,
                    action=action,
                    target=decision.target,
                    error=f"Unknown action type: {action}",
                    duration_ms=(time.monotonic() - start) * 1000,
                )

            # Wait for page to settle after any real action
            if action not in ("done", "stuck", "wait"):
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=self.SETTLE_TIMEOUT_MS)
                except Exception:
                    # Page may already be loaded; don't fail on timeout
                    pass

            # Capture page state after action and detect changes
            if state_before:
                state_after = self._capture_page_state()
                change_info = self._detect_page_change(state_before, state_after)
            else:
                # Skipped verification -- assume action had effect
                change_info = {"changed": True, "changes": []}

            # Smart retry for click actions that had no visible effect.
            # The click succeeded (no exception) but nothing changed on
            # the page.  Wait briefly for async React state updates,
            # then re-check.
            if action == "click" and not change_info.get("changed", True):
                try:
                    self._page.wait_for_timeout(500)
                    state_retry = self._capture_page_state()
                    retry_change = self._detect_page_change(state_before, state_retry)
                    if retry_change.get("changed"):
                        # Delayed effect detected
                        change_info = retry_change
                except Exception:
                    pass

            duration_ms = (time.monotonic() - start) * 1000
            return ActionResult(
                success=True,
                action=action,
                target=decision.target,
                duration_ms=round(duration_ms, 1),
                page_changed=change_info.get("changed", True),
                change_details=change_info.get("changes", []),
                sidebar_auto_dismissals=overlay_dismissals,
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return ActionResult(
                success=False,
                action=action,
                target=decision.target,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=round(duration_ms, 1),
                sidebar_auto_dismissals=overlay_dismissals,
            )

    def _do_click(self, decision: PersonaDecision) -> None:
        """Execute a click action.

        Target can be:
        - Coordinates with label: "Create account button at approximately 363, 822"
        - Text description: "the Sign Up button"
        - Pure coordinates: "x=300, y=400" or "(300, 400)"

        Strategy: ALWAYS try text/label matching first, even when coordinates are
        present. The AI often returns correct labels but DPR-scaled coordinates that
        land in the wrong spot. Text matching is more reliable when available.
        """
        target_text = decision.target.strip()

        # -- Fast-path: Sidebar / Modal Dismiss Patterns ---------------------
        # Coordinate-based clicks on close/dismiss buttons are unreliable
        # (especially on mobile DPR viewports). Detect dismiss intent from
        # the target description and use semantic selectors instead.
        target_lower_fp = target_text.lower()
        dismiss_keywords = [
            "close",
            "dismiss",
            "x button",
            "close button",
            "close navigation",
            "close menu",
            "close sidebar",
        ]
        if any(kw in target_lower_fp for kw in dismiss_keywords):
            # Strategy 1: Try aria-label based close buttons
            close_labels = [
                "Close navigation menu",
                "Close",
                "Dismiss",
                "Close navigation",
                "Close menu",
                "Close sidebar",
                "Close dialog",
                "Close modal",
            ]
            for _label in close_labels:
                try:
                    btn = self._page.get_by_label(_label)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click(timeout=5000)
                        self._page.wait_for_timeout(300)
                        return
                except Exception:
                    continue

            # Strategy 2: Try clicking the backdrop overlay
            try:
                backdrop = self._page.locator(
                    ".fixed.inset-0.bg-black\\/60, "
                    '.fixed.inset-0[aria-hidden="true"], '
                    "[data-backdrop], "
                    ".overlay, .backdrop"
                )
                if backdrop.count() > 0 and backdrop.first.is_visible():
                    backdrop.first.click(timeout=5000, force=True)
                    self._page.wait_for_timeout(300)
                    return
            except Exception:
                pass

            # Strategy 3: Press Escape key (most modals/sidebars respond to Escape)
            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(300)
                return
            except Exception:
                pass

        # -- Fast-path: Hamburger / Menu Open Patterns -----------------------
        # Similarly, "open menu" / "hamburger" targets should use semantic
        # selectors rather than coordinate clicks.
        open_keywords = [
            "hamburger",
            "open menu",
            "open navigation",
            "toggle menu",
            "nav menu",
            "menu button",
            "navigation button",
        ]
        if any(kw in target_lower_fp for kw in open_keywords):
            open_labels = [
                "Open navigation menu",
                "Toggle menu",
                "Menu",
                "Open menu",
                "Navigation",
                "Toggle navigation",
                "Navigation menu",
                "Open navigation",
            ]
            for _label in open_labels:
                try:
                    btn = self._page.get_by_label(_label)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click(timeout=5000)
                        self._page.wait_for_timeout(300)
                        return
                except Exception:
                    continue

        # Step 1: Extract a button/link label from the target description.
        # Handles patterns like:
        #   "Create account button at approximately 363, 822" -> "Create account"
        #   "the Next button" -> "Next"
        #   "Sign in link" -> "Sign in"
        label: str | None = None
        label_match = re.match(
            r"^(?:the\s+)?(.+?)\s+(?:button|btn|link|option|card|item|tile|element|tab|choice|selector|icon)\b",
            target_text,
            re.I,
        )
        if label_match:
            label = label_match.group(1).strip()

        # Step 1b: Extract clean text by stripping coordinate suffixes.
        # Handles "Some text at approximately 362, 527" -> "Some text"
        # and "Some text at (300, 400)" -> "Some text"
        clean_target: str | None = None
        coord_suffix = re.match(
            r"^(.+?)\s+(?:at\s+(?:approximately|around|near|about|position)?\s*[\d(])",
            target_text,
            re.I,
        )
        if coord_suffix:
            raw = coord_suffix.group(1).strip()
            # Strip element type words from the end: "Ground-Up Construction option" -> "Ground-Up Construction"
            clean_target = re.sub(
                r"\s+(?:button|btn|link|option|card|item|tile|element|tab|choice|selector|icon)$", "", raw, flags=re.I
            ).strip()
            if not clean_target:
                clean_target = raw  # Don't lose the text if stripping removed everything

        # Step 1c: Normalize target -- strip Unicode arrows/symbols and split on
        # contextual prepositions.
        # Handles targets like:
        #   "Get started -> under 3. Set budget" -> primary="Get started", context="3. Set budget"
        #   "Get started -> link under 3. Set budget at 238, 988"
        #       -> primary="Get started", context="3. Set budget at 238, 988"
        #   "Get started -> at 238, 988" -> primary="Get started", context=None (coordinates only)
        context_preps = re.compile(
            r"\s+(?:under|below|next\s+to|near|beside|above|on\s+the|in\s+the|within|inside|for|of)\s+",
            re.I,
        )
        # Strip common Unicode arrows and decorative characters
        unicode_arrow_re = r"[\u2190-\u21FF\u25A0-\u25FF\u2700-\u27BF\u2013\u2014\u2022\u00D7]"
        normalized = re.sub(unicode_arrow_re, "", target_text).strip()
        normalized = re.sub(r"\s{2,}", " ", normalized)  # collapse multiple spaces

        context_parts = context_preps.split(normalized, maxsplit=1)
        primary_text = context_parts[0].strip() if context_parts else normalized
        context_text = context_parts[1].strip() if len(context_parts) > 1 else None
        # Strip trailing element-type words from primary (e.g. "Get started link" -> "Get started")
        primary_text = re.sub(
            r"\s+(?:button|btn|link|option|card|item|tile|element|tab|choice|selector|icon)$",
            "",
            primary_text,
            flags=re.I,
        ).strip()
        # If primary_text collapsed to empty or equals the full target, don't use it
        if not primary_text or primary_text == target_text:
            primary_text = None

        # Step 2: Try text/label-based matching first (preferred -- immune to DPR issues).
        # Use the extracted label when available, otherwise try the full target text.
        candidates = []
        if label:
            candidates.append(label)
        if primary_text and primary_text not in candidates:
            candidates.append(primary_text)
        if clean_target and clean_target not in candidates:
            candidates.append(clean_target)
        if normalized and normalized not in candidates and normalized != target_text:
            candidates.append(normalized)
        candidates.append(target_text)

        # Step 1d: Generate additional candidate variants for icon/symbol matching.
        # AI personas often describe buttons with icon prefixes (e.g. "+ Add") or
        # suffixes (e.g. "Set up budget ->") but the accessible name only contains
        # the text portion (e.g. "Add", "Set up budget"). Strip leading/trailing
        # punctuation and symbols to produce additional candidates.
        leading_symbols = re.compile(r"^[\+\-\×\·\•\→\←\>\<\☰\✕\✖\✗\✘\≡\⋮\⊕\⊖\▶\▸\◀\◂\s]+")
        trailing_symbols = re.compile(r"[\+\-\×\·\•\→\←\>\<\✕\✖\✗\✘\▶\▸\◀\◂\s]+$")
        extra_candidates: list[str] = []
        for c in list(candidates):
            # Strip leading symbols
            stripped_leading = leading_symbols.sub("", c).strip()
            is_new_leading = (
                stripped_leading
                and stripped_leading != c
                and stripped_leading not in candidates
                and stripped_leading not in extra_candidates
            )
            if is_new_leading:
                extra_candidates.append(stripped_leading)
            # Strip trailing symbols
            stripped_trailing = trailing_symbols.sub("", c).strip()
            is_new_trailing = (
                stripped_trailing
                and stripped_trailing != c
                and stripped_trailing not in candidates
                and stripped_trailing not in extra_candidates
            )
            if is_new_trailing:
                extra_candidates.append(stripped_trailing)
            # Strip both
            stripped_both = trailing_symbols.sub("", leading_symbols.sub("", c)).strip()
            is_new_both = (
                stripped_both
                and stripped_both != c
                and stripped_both not in candidates
                and stripped_both not in extra_candidates
            )
            if is_new_both:
                extra_candidates.append(stripped_both)

        # Insert symbol-stripped variants right after the originals (higher priority than icon mappings)
        candidates.extend(extra_candidates)

        # Step 1e: Icon-description to aria-label mappings.
        # AI personas describe icon-only buttons visually (e.g. "hamburger menu icon")
        # but the actual aria-label is different (e.g. "Open navigation menu").
        # Add common aria-label variants as fallback candidates.
        icon_aria_mappings: dict[re.Pattern, list[str]] = {
            re.compile(r"hamburger|three.?line|menu\s*icon|nav(?:igation)?\s*icon|toggle\s*menu", re.I): [
                "Open navigation menu",
                "Close navigation menu",
                "Toggle menu",
                "Toggle navigation",
                "Menu",
                "Navigation menu",
                "Open menu",
                "Close menu",
            ],
            re.compile(r"close\s*(?:button|icon|modal)?|x\s*(?:button|icon)|dismiss", re.I): [
                "Close",
                "Dismiss",
                "Close dialog",
                "Close modal",
            ],
            re.compile(r"search\s*(?:button|icon)?", re.I): [
                "Search",
                "Toggle search",
                "Open search",
            ],
            re.compile(
                r"profile\s*(?:button|icon)?|avatar|user\s*(?:button|icon|menu)?|account\s*(?:button|icon)?",
                re.I,
            ): [
                "Profile",
                "User menu",
                "Account",
                "Account menu",
                "Open profile",
            ],
            re.compile(r"settings?\s*(?:button|icon)?|gear\s*(?:button|icon)?|cog\s*(?:button|icon)?", re.I): [
                "Settings",
                "Open settings",
                "Preferences",
            ],
            re.compile(r"notification|bell\s*(?:button|icon)?", re.I): [
                "Notifications",
                "View notifications",
                "Toggle notifications",
            ],
            re.compile(r"(?:add|plus|new)\s*(?:button|icon)?", re.I): [
                "Add",
                "Create",
                "New",
                "Add new",
            ],
            re.compile(r"(?:delete|trash|remove)\s*(?:button|icon)?", re.I): [
                "Delete",
                "Remove",
                "Trash",
            ],
            re.compile(r"(?:edit|pencil|pen)\s*(?:button|icon)?", re.I): [
                "Edit",
                "Modify",
            ],
            re.compile(r"(?:more|dots|ellipsis|three\s*dots?|kebab|overflow)\s*(?:button|icon|menu)?", re.I): [
                "More",
                "More options",
                "Actions",
                "Options",
            ],
            re.compile(r"(?:back|arrow.?left|go\s*back)\s*(?:button|icon)?", re.I): [
                "Back",
                "Go back",
                "Navigate back",
            ],
            re.compile(r"(?:expand|collapse|chevron|arrow.?down|caret)\s*(?:button|icon)?", re.I): [
                "Expand",
                "Collapse",
                "Toggle",
            ],
        }
        icon_candidates: list[str] = []
        for pattern, aria_labels in icon_aria_mappings.items():
            if pattern.search(target_text):
                for aria in aria_labels:
                    if aria not in candidates and aria not in icon_candidates:
                        icon_candidates.append(aria)
        candidates.extend(icon_candidates)

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        candidates = deduped

        # Step 2: Modal-aware text/label matching.
        # When a modal dialog is open, search inside it first to avoid clicking
        # identically-named buttons behind the modal backdrop.
        scope = self._get_active_scope()

        for candidate in candidates:
            locator = scope.get_by_role("button", name=candidate)
            if locator.count() > 0:
                locator.first.click()
                return
            locator = scope.get_by_role("tab", name=candidate)
            if locator.count() > 0:
                locator.first.click()
                return
            locator = scope.get_by_role("link", name=candidate)
            if locator.count() > 0:
                # If multiple matches, prefer one that's visible in the viewport
                if locator.count() > 1:
                    for i in range(locator.count()):
                        try:
                            box = locator.nth(i).bounding_box(timeout=2000)
                            if box and 0 <= box["x"] < self._css_width and 0 <= box["y"] < self._css_height:
                                locator.nth(i).click()
                                return
                        except Exception:
                            continue
                locator.first.click()
                return
            locator = scope.get_by_text(candidate, exact=False)
            if locator.count() > 0:
                locator.first.click()
                return
            # Try aria-label matching -- catches elements whose accessible name
            # differs from their visible text (e.g. icon-only buttons with aria-label).
            locator = scope.get_by_label(candidate)
            if locator.count() > 0:
                locator.first.click()
                return

        # If we were searching inside a modal and found nothing, also try a
        # submit button inside the modal (for forms whose submit button label
        # doesn't match the candidate text).
        if scope != self._page:
            submit_btn = scope.locator('button[type="submit"]:visible')
            if submit_btn.count() > 0:
                submit_btn.first.click()
                return

        # If modal scope found nothing, fall back to full page scope.
        # Use force=True because the element may be partially obscured.
        if scope != self._page:
            for candidate in candidates:
                locator = self._page.get_by_role("button", name=candidate)
                if locator.count() > 0:
                    locator.first.click(force=True)
                    return
                locator = self._page.get_by_role("tab", name=candidate)
                if locator.count() > 0:
                    locator.first.click(force=True)
                    return
                locator = self._page.get_by_role("link", name=candidate)
                if locator.count() > 0:
                    # If multiple matches, prefer one that's visible in the viewport
                    if locator.count() > 1:
                        for i in range(locator.count()):
                            try:
                                box = locator.nth(i).bounding_box(timeout=2000)
                                if box and 0 <= box["x"] < self._css_width and 0 <= box["y"] < self._css_height:
                                    locator.nth(i).click(force=True)
                                    return
                            except Exception:
                                continue
                    locator.first.click(force=True)
                    return
                locator = self._page.get_by_text(candidate, exact=False)
                if locator.count() > 0:
                    locator.first.click(force=True)
                    return
                # Try aria-label matching on full page scope
                locator = self._page.get_by_label(candidate)
                if locator.count() > 0:
                    locator.first.click(force=True)
                    return

        # Step 2b: Contextual disambiguation -- when primary text matches multiple
        # elements on the page, use the context text to pick the right one.
        # E.g. target "Get started -> under 3. Set budget" -> find the "Get started"
        # link/button whose ancestor contains "Set budget".
        if primary_text and context_text:
            # Strip any trailing coordinate info from context for cleaner text matching
            clean_context = re.sub(
                r"\s+(?:at\s+(?:approximately|around|near|about|position)?\s*[\d(]).*$",
                "",
                context_text,
                flags=re.I,
            ).strip()
            if not clean_context:
                clean_context = context_text

            try:
                # Use JavaScript to find element by text with context in ancestor
                element = self._page.evaluate_handle(
                    """([primary, context]) => {
                        const els = document.querySelectorAll('a, button, [role="button"], [role="link"]');
                        for (const el of els) {
                            const text = (el.textContent || '').trim();
                            if (!text.toLowerCase().includes(primary.toLowerCase())) continue;
                            let parent = el.parentElement;
                            for (let i = 0; i < 8 && parent; i++) {
                                if ((parent.textContent || '').toLowerCase().includes(context.toLowerCase())) {
                                    return el;
                                }
                                parent = parent.parentElement;
                            }
                        }
                        return null;
                    }""",
                    [primary_text, clean_context],
                )
                js_element = element.as_element()
                if js_element is not None:
                    js_element.click()
                    return
            except Exception:
                pass

        # Step 3: Try exact text match on the full target (safe via Playwright API).
        # Use modal scope if available.
        try:
            locator = scope.get_by_text(target_text, exact=True)
            if locator.count() > 0:
                locator.first.click()
                return
        except Exception:
            pass  # selector error from unusual text; fall through

        # Step 3b: Extract clickable text from the observation field as fallback.
        # When the target is pure coordinates but the observation mentions a button
        # by name (e.g. "I see the 'Continue to Brief' button"), try to find it.
        # Search within modal scope first, then fall back to page.
        if decision.observation:
            obs_matches = re.findall(r"['\"]([^'\"]{2,50})['\"]", decision.observation)
            _skip_words = frozenset({"css", "i", "a", "the", "at", "in", "on", "is", "it", "to"})
            for obs_text in obs_matches:
                if obs_text.lower() in _skip_words:
                    continue
                locator = scope.get_by_role("button", name=obs_text)
                if locator.count() > 0:
                    locator.first.click()
                    return
                locator = scope.get_by_role("link", name=obs_text)
                if locator.count() > 0:
                    locator.first.click()
                    return
                locator = scope.get_by_text(obs_text, exact=False)
                if locator.count() > 0:
                    locator.first.click()
                    return

        # Step 3c: Icon-only button detection.
        # Buttons with only SVG icons (e.g. <Send />) have no visible text for
        # get_by_role or get_by_text to match. When the target mentions "send" or
        # "submit", try finding form submit buttons or nearby icon buttons.
        # Search within modal scope first.
        target_lower = target_text.lower()
        if any(kw in target_lower for kw in ["send", "submit"]):
            # Try finding a submit button inside a form
            submit_btn = scope.locator('form button[type="submit"]:visible')
            if submit_btn.count() > 0:
                submit_btn.first.click()
                return
            # Try any button inside a form (some forms use default type)
            form_btn = scope.locator("form button:visible")
            if form_btn.count() > 0:
                form_btn.first.click()
                return

        # Step 3d: <select> dropdown detection.
        # In headless Chromium, clicking a <select> doesn't render a visible
        # dropdown -- the agent gets stuck. If the target describes a dropdown
        # and a value is provided, find the <select> and select the option.
        # Search within modal scope first.
        if decision.value:
            try:
                selects = scope.locator("select:visible")
                select_count = selects.count()
                if select_count > 0:
                    select_stop_words = frozenset(
                        {
                            "select",
                            "dropdown",
                            "drop",
                            "down",
                            "option",
                            "choose",
                            "pick",
                            "the",
                            "for",
                            "and",
                            "from",
                            "with",
                            "click",
                            "button",
                            "menu",
                            "list",
                        }
                    )
                    target_words = [
                        w.lower() for w in re.findall(r"\b\w{3,}\b", target_text) if w.lower() not in select_stop_words
                    ]
                    if target_words:
                        for i in range(select_count):
                            sel = selects.nth(i)
                            sel_id = (sel.get_attribute("id") or "").lower()
                            sel_name = (sel.get_attribute("name") or "").lower()
                            sel_aria = (sel.get_attribute("aria-label") or "").lower()
                            label_text = ""
                            if sel_id:
                                try:
                                    label_loc = scope.locator(f'label[for="{sel_id}"]')
                                    if label_loc.count() > 0:
                                        label_text = label_loc.first.inner_text().lower()
                                except Exception:
                                    pass
                            combined = f"{label_text} {sel_id} {sel_name} {sel_aria}"
                            if any(word in combined for word in target_words):
                                self._select_option_fuzzy(sel, decision.value)
                                return
            except Exception:
                pass  # Best-effort; fall through to coordinate click

        # Step 4: Fall back to coordinate-based clicking only if text matching failed.
        # This handles pure-coordinate targets and cases where no element was found by text.
        coords = self._parse_coordinates(target_text)
        if coords:
            x, y = coords
            x_css, y_css = self._to_css_coords(x, y)
            self._page.mouse.click(x_css, y_css)
            # After coordinate click, check if we landed on a <select> and a value
            # was provided -- if so, auto-select the option instead of leaving the
            # agent to deal with an invisible dropdown.
            if decision.value:
                try:
                    focused_tag = self._page.evaluate(
                        "() => document.activeElement ? document.activeElement.tagName.toLowerCase() : ''"
                    )
                    if focused_tag == "select":
                        focused_el = self._page.locator(":focus").first
                        self._select_option_fuzzy(focused_el, decision.value)
                except Exception:
                    pass  # Best-effort; the click already happened
            return

        raise ValueError(f"Could not find clickable element matching: {target_text}")

    def _fill_and_dispatch(self, element, value: str) -> None:
        """Fill an element and dispatch React-compatible events.

        After Playwright's .fill(), dispatch 'input' and 'change' events to
        ensure React controlled inputs (which rely on synthetic events from
        onChange handlers) actually update their state.

        If the element's value still doesn't match after fill + events (e.g.
        React resets it), fall back to clearing the field and using
        keyboard.type() which simulates real keypresses.
        """
        element.fill(value)
        # Dispatch events for React controlled inputs
        element.dispatch_event("input", {"bubbles": True})
        element.dispatch_event("change", {"bubbles": True})

        # Verify fill worked -- React may have reset the value
        try:
            actual_value = element.evaluate("el => el.value")
            if actual_value != value:
                # Fall back to keyboard typing which simulates real keypresses
                element.click()
                element.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }")
                self._page.keyboard.type(value, delay=10)
        except Exception:
            pass  # Best-effort verification; don't fail the fill

    def _smart_fill(self, locator, value: str) -> None:
        """Fill an element, handling both regular inputs and <select> dropdowns.

        For <select> elements, Playwright's .fill() does not work -- we must use
        select_option() instead.  This method detects the element type and routes
        to the appropriate interaction:

        - <select>: tries exact label match, then fuzzy (case-insensitive partial)
          match on option text, then falls back to matching by option value.
        - <input>/<textarea>: delegates to _fill_and_dispatch() with React event
          support.

        This is the primary entry point for filling any form element found by the
        strategy chain in _do_fill().
        """
        element = locator.first
        try:
            tag_name = element.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            # If we can't determine the tag, assume it's a regular input
            self._fill_and_dispatch(element, value)
            return

        if tag_name == "select":
            self._select_option_fuzzy(element, value)
            return

        # Handle date inputs -- Playwright .fill() requires ISO format YYYY-MM-DD
        if tag_name == "input":
            input_type = element.evaluate("el => el.type")
            if input_type == "date":
                iso_value = self._normalize_date_value(value)
                self._fill_date_input(element, iso_value)
                return

        # Regular input/textarea
        self._fill_and_dispatch(element, value)

    def _fill_date_input(self, element, iso_value: str) -> None:
        """Fill an <input type="date"> with an ISO YYYY-MM-DD value.

        Playwright's fill() accepts ISO format for date inputs, but some
        implementations (e.g. React controlled date inputs) may reset the value
        after fill.  Strategy:

        1. Try element.fill(iso_value) -- the standard Playwright path.
        2. Dispatch 'input' and 'change' events so React controlled inputs update.
        3. Verify the value was accepted.  If not, fall back to direct DOM
           assignment via evaluate() which bypasses React reconciliation.
        """
        try:
            element.fill(iso_value)
            element.dispatch_event("input", {"bubbles": True})
            element.dispatch_event("change", {"bubbles": True})
            # Verify the value stuck
            actual = element.evaluate("el => el.value")
            if actual == iso_value:
                return
        except Exception:
            pass

        # Fallback: set value directly via JavaScript and fire events
        try:
            element.evaluate(
                "(el, val) => {"
                "  el.value = val;"
                "  el.dispatchEvent(new Event('input', {bubbles: true}));"
                "  el.dispatchEvent(new Event('change', {bubbles: true}));"
                "}",
                iso_value,
            )
        except Exception:
            pass  # Best-effort; don't fail the overall fill action

    def _normalize_date_value(self, value: str) -> str:
        """Convert common date formats to ISO YYYY-MM-DD for Playwright date inputs.

        Handles:
        - MM/DD/YYYY (e.g., "05/15/2026")
        - M/D/YYYY (e.g., "5/15/2026")
        - YYYY-MM-DD (already ISO, pass through)
        - Month DD, YYYY (e.g., "May 15, 2026")
        - DD Month YYYY (e.g., "15 May 2026")
        """
        from datetime import datetime

        value = value.strip()

        # Already ISO format
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value

        # MM/DD/YYYY or M/D/YYYY
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
        if m:
            month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{year:04d}-{month:02d}-{day:02d}"

        # Try common formats with datetime.strptime
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%m-%d-%Y"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # Fallback: return as-is and hope for the best
        return value

    def _select_option_fuzzy(self, element, value: str) -> None:
        """Select an option from a <select> element with fuzzy label matching.

        Strategy chain:
        1. Exact label match via select_option(label=value)
        2. Case-insensitive partial text match on option labels
        3. Direct value string match via select_option(value=value)
        4. Last resort: exact label match (may raise if nothing works)
        """
        # 1. Try exact label match
        try:
            element.select_option(label=value)
            return
        except Exception:
            pass

        # 2. Fuzzy match: find option whose text contains the value (or vice versa)
        try:
            options = element.evaluate("""el => {
                return Array.from(el.options).map(o => ({
                    value: o.value,
                    text: o.textContent.trim()
                }));
            }""")
            value_lower = value.lower()
            for opt in options:
                opt_text_lower = opt["text"].lower()
                if value_lower in opt_text_lower or opt_text_lower in value_lower:
                    element.select_option(value=opt["value"])
                    return
            # 3. Try selecting by value string directly
            element.select_option(value=value)
            return
        except Exception:
            pass

        # 4. Last resort: label match (let it raise if nothing works)
        element.select_option(label=value)

    def _do_fill(self, decision: PersonaDecision) -> None:
        """Execute a fill action -- find an input/select and type/select into it.

        Tries multiple strategies in order to handle verbose AI descriptions like
        "Project Name input field with placeholder 'e.g., 450 Main St Development'":

        1.  Exact label match (fast path for well-labelled fields)
        1b. <select> by label/id/name keyword match (handles dropdowns)
        2.  Quoted strings extracted from target -> get_by_placeholder()
        3.  Full target text -> get_by_placeholder() (existing behaviour)
        4.  Label-like prefix extracted from "X input field..." pattern -> get_by_label()
        5.  Coordinate-based click-and-type
        6.  CSS attribute selector fallback
        7.  Keyword scan across all visible inputs / textareas / selects
        """
        target_text = decision.target.strip()
        value = decision.value or ""

        # Modal-aware scoping: when a modal dialog is open, search inside it
        # first to find form fields in the modal instead of behind it.
        scope = self._get_active_scope()

        # 1. Try exact label match first
        locator = scope.get_by_label(target_text)
        if locator.count() > 0:
            self._smart_fill(locator, value)
            return

        # 1a. Try partial label match -- strip description suffixes.
        # Handles "Budgeted Amount ($) input field with placeholder 0.00"
        #       -> try get_by_label("Budgeted Amount ($)")
        desc_suffix = re.match(
            r"^(.+?)\s+(?:input|field|text|box|area|textarea|select|dropdown|with|showing|at\s+\d)",
            target_text,
            re.I,
        )
        if desc_suffix:
            partial_label = desc_suffix.group(1).strip()
            if partial_label != target_text:
                locator = scope.get_by_label(partial_label, exact=False)
                if locator.count() > 0:
                    self._smart_fill(locator, value)
                    return

        # 1b. Try finding a <select> by its label, id, or name.
        # Handles cases like "Budget Category dropdown", "Category select", etc.
        # In headless Chromium, clicking a <select> doesn't render a visible
        # dropdown -- we must use select_option() directly.
        try:
            selects = scope.locator("select:visible")
            select_count = selects.count()
            if select_count > 0:
                # Build keywords from target, excluding generic words
                select_stop_words = frozenset(
                    {
                        "select",
                        "dropdown",
                        "drop",
                        "down",
                        "option",
                        "choose",
                        "pick",
                        "the",
                        "for",
                        "and",
                        "from",
                        "with",
                        "input",
                        "field",
                        "menu",
                        "list",
                    }
                )
                target_words = [
                    w.lower() for w in re.findall(r"\b\w{3,}\b", target_text) if w.lower() not in select_stop_words
                ]
                if target_words:
                    for i in range(select_count):
                        sel = selects.nth(i)
                        sel_id = (sel.get_attribute("id") or "").lower()
                        sel_name = (sel.get_attribute("name") or "").lower()
                        sel_aria = (sel.get_attribute("aria-label") or "").lower()
                        # Find associated <label> text via the for= attribute
                        label_text = ""
                        if sel_id:
                            try:
                                label_loc = scope.locator(f'label[for="{sel_id}"]')
                                if label_loc.count() > 0:
                                    label_text = label_loc.first.inner_text().lower()
                            except Exception:
                                pass
                        combined = f"{label_text} {sel_id} {sel_name} {sel_aria}"
                        if any(word in combined for word in target_words):
                            self._select_option_fuzzy(sel, value)
                            return
        except Exception:
            pass  # <select> detection is best-effort; fall through to other strategies

        # 2. Extract quoted strings (e.g. 'e.g., 450 Main St') and try as placeholder
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", target_text)
        for q in quoted:
            locator = scope.get_by_placeholder(q, exact=False)
            if locator.count() > 0:
                self._smart_fill(locator, value)
                return

        # 3. Try placeholder matching with the full target text
        locator = scope.get_by_placeholder(target_text)
        if locator.count() > 0:
            self._smart_fill(locator, value)
            return

        # 4. Extract label-like prefix: "Project Name input field..." -> "Project Name"
        #    Uses .+? (any char, lazy) so labels with special chars like ($) are captured.
        label_match = re.match(
            r"^(.+?)\s+(?:input|field|text\s*(?:box|area|field)|box|area|textarea|select|dropdown)\b",
            target_text,
            re.I,
        )
        if label_match:
            label_text = label_match.group(1).strip()
            locator = scope.get_by_label(label_text, exact=False)
            if locator.count() > 0:
                self._smart_fill(locator, value)
                return

        # 5. Try coordinate-based approach if target looks like coords
        coords = self._parse_coordinates(target_text)
        if coords:
            x, y = coords
            x_css, y_css = self._to_css_coords(x, y)
            self._page.mouse.click(x_css, y_css)
            # After clicking, check what kind of element received focus
            try:
                focused_info = self._page.evaluate(
                    "() => {"
                    "  const el = document.activeElement;"
                    "  if (!el) return {tag: '', type: ''};"
                    "  return {tag: el.tagName.toLowerCase(), type: el.type || ''};"
                    "}"
                )
                focused_tag = focused_info.get("tag", "") if isinstance(focused_info, dict) else ""
                focused_type = focused_info.get("type", "") if isinstance(focused_info, dict) else ""
                if focused_tag == "select":
                    focused_el = self._page.locator(":focus").first
                    self._select_option_fuzzy(focused_el, value)
                    return
                if focused_tag == "input" and focused_type == "date":
                    focused_el = self._page.locator(":focus").first
                    iso_value = self._normalize_date_value(value)
                    self._fill_date_input(focused_el, iso_value)
                    return
            except Exception:
                pass
            self._page.keyboard.type(value)
            return

        # 6. CSS attribute selector fallback
        # Sanitize text for CSS selector interpolation to prevent injection
        safe_text = self._sanitize_for_selector(target_text)
        for selector in [
            f'input[name*="{safe_text}" i]',
            f'input[placeholder*="{safe_text}" i]',
            f'textarea[name*="{safe_text}" i]',
            f'textarea[placeholder*="{safe_text}" i]',
            f'input[aria-label*="{safe_text}" i]',
            f'select[name*="{safe_text}" i]',
            f'select[aria-label*="{safe_text}" i]',
        ]:
            locator = scope.locator(selector)
            if locator.count() > 0:
                self._smart_fill(locator, value)
                return

        # 7. Last resort: keyword scan over all visible inputs / textareas / selects.
        # Build a set of meaningful keywords from the target description, ignoring
        # generic field-related words that would match almost anything.
        stop_words = frozenset(
            {
                "input",
                "field",
                "text",
                "with",
                "the",
                "for",
                "and",
                "placeholder",
                "area",
                "textarea",
                "box",
                "label",
                "enter",
                "type",
                "value",
                "select",
                "dropdown",
                "option",
                "choose",
                "showing",
            }
        )
        keywords = [w.lower() for w in re.findall(r"\b\w{3,}\b", target_text) if w.lower() not in stop_words]
        if keywords:
            inputs = scope.locator("input:visible, textarea:visible, select:visible")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                aria_label = (inp.get_attribute("aria-label") or "").lower()
                name = (inp.get_attribute("name") or "").lower()
                combined = f"{placeholder} {aria_label} {name}"
                for kw in keywords:
                    if kw in combined:
                        self._smart_fill(inp, value)
                        return

        raise ValueError(f"Could not find fillable element matching: {target_text}")

    def _do_navigate(self, decision: PersonaDecision) -> None:
        """Navigate to a URL."""
        url = decision.value or decision.target

        # Validate that the target looks like a URL or path, not garbage input
        if not url or not url.strip():
            raise ValueError("Navigate target is empty")

        url = url.strip()

        # Reject coordinate-like targets (e.g., "195,420", "0.584,0.672")
        if re.match(r"^[\d.,\s]+$", url):
            raise ValueError(f"Navigate target looks like coordinates, not a URL: {url}")

        # Reject descriptive text that isn't a URL (no path separators, no domain)
        if not any(c in url for c in ("/", ".")) and not url.startswith(("http", "/")):
            raise ValueError(f"Navigate target doesn't look like a URL: {url}")

        if not url.startswith(("http://", "https://")):
            if not url.startswith("/"):
                url = f"/{url}"
            # Relative URL -- prepend current page's origin
            try:
                origin = self._page.evaluate("() => window.location.origin")
                url = origin + url
            except Exception:
                url = f"http://localhost:3000{url}"  # fallback
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.SETTLE_TIMEOUT_MS)

    def _do_scroll(self, decision: PersonaDecision) -> None:
        """Scroll the page, targeting the actual scroll container.

        Many modern apps use a nested scroll container like
        ``<main class="overflow-y-auto">`` inside an ``overflow: hidden`` outer
        div.  In that layout ``window.scrollBy()`` and ``mouse.wheel()`` on the
        window have no effect because the *window* isn't the scrolling element.

        Strategy:
        1. Find a ``<main>`` element whose scrollHeight exceeds its clientHeight
           (i.e. it is actually scrollable).
        2. Fall back to ``document.scrollingElement`` or ``document.documentElement``.
        3. Call ``element.scrollBy()`` on the resolved target.
        """
        # Parse direction from target description
        target_lower = (decision.target or "").lower()
        delta_y = 300  # Default scroll amount
        if "up" in target_lower:
            delta_y = -300
        elif "down" in target_lower:
            delta_y = 300
        elif "bottom" in target_lower:
            delta_y = 600
        elif "top" in target_lower:
            delta_y = -600

        # Check for explicit pixel amounts
        px_match = re.search(r"(\d+)\s*(?:px|pixels?)", target_lower)
        if px_match:
            amount = int(px_match.group(1))
            if "up" in target_lower or "top" in target_lower:
                delta_y = -amount
            else:
                delta_y = amount

        # Use JavaScript to scroll the correct container -- mouse.wheel only
        # works when the mouse happens to be over the scroll container, and
        # window.scrollBy does nothing when body has overflow:hidden.
        self._page.evaluate(
            """(amount) => {
                const main = document.querySelector('main[class*="overflow"]')
                    || document.querySelector('main');
                const target = (main && main.scrollHeight > main.clientHeight)
                    ? main
                    : (document.scrollingElement || document.documentElement);
                target.scrollBy({ top: amount, behavior: 'instant' });
            }""",
            delta_y,
        )

    def _do_keyboard(self, decision: PersonaDecision) -> None:
        """Press a keyboard key or key combination."""
        key = decision.value or decision.target
        if not key:
            raise ValueError("No key specified for keyboard action")
        self._page.keyboard.press(key)

    def _do_wait(self, decision: PersonaDecision) -> None:
        """Wait for a specified duration."""
        # Parse duration from target/value
        ms = 1000  # Default 1 second
        for text in [decision.value, decision.target]:
            if text:
                num_match = re.search(r"(\d+)", text)
                if num_match:
                    val = int(num_match.group(1))
                    # If the number looks like milliseconds (>100), use as-is
                    # Otherwise treat as seconds
                    ms = val if val > 100 else val * 1000
                    break
        self._page.wait_for_timeout(ms)

    @staticmethod
    def _parse_coordinates(text: str) -> tuple[float, float] | None:
        """Try to extract x,y coordinates from a target description.

        Handles formats like:
        - "x=300, y=400"  (explicit -- always matched)
        - "(300, 400)"    (parenthesized -- high confidence)
        - "300, 400"      (bare -- only in short, non-ambiguous text)
        """
        if not text:
            return None

        # Pattern 1: Explicit x=NNN, y=NNN -- always reliable
        m = re.search(
            r"x\s*[=:]\s*(\d+(?:\.\d+)?)\s*[,;]\s*y\s*[=:]\s*(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        # Pattern 1.5: "approximately X, Y" or "at X, Y" or similar -- high confidence
        m = re.search(
            r"(?:approximately|at|around|near|position)\s+(\d{2,4}(?:\.\d+)?)\s*[,]\s*(\d{2,4}(?:\.\d+)?)",
            text,
            re.I,
        )
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            if 0 <= x <= 3000 and 0 <= y <= 3000:
                return x, y

        # Pattern 2: Parenthesized coordinates like "(300, 400)" -- high confidence
        m = re.search(
            r"\(\s*(\d{2,4}(?:\.\d+)?)\s*,\s*(\d{2,4}(?:\.\d+)?)\s*\)",
            text,
        )
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            if 0 <= x <= 3000 and 0 <= y <= 3000:
                return x, y

        # Pattern 3: Bare "NNN, NNN" -- only if the text is short and coordinate-like
        # Skip if text contains words that indicate non-coordinate context
        false_positive_words = re.compile(
            r"\b(row|column|col|page|item|step|action|version|section|"
            r"line|index|count|total|number|num|size|width|height)\b",
            re.I,
        )
        if len(text) < 60 and not false_positive_words.search(text):
            m = re.search(
                r"(\d{2,4}(?:\.\d+)?)\s*,\s*(\d{2,4}(?:\.\d+)?)",
                text,
            )
            if m:
                x, y = float(m.group(1)), float(m.group(2))
                if 0 <= x <= 3000 and 0 <= y <= 3000:
                    return x, y

        return None
