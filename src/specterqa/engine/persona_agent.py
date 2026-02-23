"""SpecterQA Persona Agent — AI persona that sees and uses the UI.

Receives a persona profile, current goal, and a screenshot. Builds a system
prompt from the persona, sends the screenshot to a Claude vision model, and
returns a structured action decision.

Supports tiered model routing:
- Haiku for simple navigation actions (click, scroll, wait)
- Sonnet for complex actions (fill, initial assessment, periodic checkpoints)
- Local Ollama fallback for zero-cost simple actions
"""

from __future__ import annotations

import json
import logging
from typing import Any

from specterqa.engine.action_executor import PersonaDecision
from specterqa.engine.cost_tracker import CostTracker
from specterqa.models import MODELS

logger = logging.getLogger("specterqa.engine.persona_agent")

# Model ID lookup — maps tier names to model IDs from specterqa.models
MODEL_IDS: dict[str, str] = {
    "haiku": MODELS.get("persona_simple", "claude-haiku-4-5-20251001"),
    "sonnet": MODELS.get("persona_complex", "claude-sonnet-4-20250514"),
    "opus": MODELS.get("persona_heavy", "claude-opus-4-20250115"),
}

# JSON schema for persona response — used in system prompt
RESPONSE_SCHEMA = """{
  "observation": "What I see on the screen",
  "action": "click|fill|navigate|scroll|keyboard|wait|done|stuck",
  "target": "Description of what to click/fill (for click: approximate x,y coordinates)",
  "value": "Text to type (for fill actions), URL (for navigate), key (for keyboard)",
  "reasoning": "Why I'm taking this action",
  "ux_notes": "Any usability observations, confusion, or feedback (or null)",
  "checkpoint": "Name of checkpoint reached (or null)",
  "goal_achieved": false
}"""

# Maximum consecutive "stuck" responses before declaring failure
MAX_CONSECUTIVE_STUCK = 3

# Maximum consecutive API failures before declaring failure
# Be more patient with API issues than with actual agent stuckness
MAX_CONSECUTIVE_API_FAILURES = 10

# Maximum number of recent screenshots to keep as full base64 in conversation history.
# Older screenshots are replaced with a text summary to prevent unbounded cost growth.
MAX_SCREENSHOT_HISTORY = 3

# Actions considered "simple" for model routing — these don't need the expensive
# screenshot_interpretation model if the previous action was also simple.
SIMPLE_ACTIONS = frozenset({"click", "navigate", "scroll", "wait", "done", "stuck"})


class PersonaAgent:
    """AI persona agent that interprets screenshots and decides on actions.

    Uses Claude vision models via the Anthropic Python SDK. Maintains
    conversation history within a browser step for context continuity.
    """

    def __init__(
        self,
        persona: dict[str, Any],
        viewport_name: str,
        viewport_size: tuple[int, int],
        cost_tracker: CostTracker,
        api_key: str | None = None,
    ) -> None:
        self._persona = persona
        self._viewport_name = viewport_name
        self._viewport_size = viewport_size
        self._cost_tracker = cost_tracker
        self._api_key = api_key
        self._conversation_history: list[dict[str, Any]] = []
        self._consecutive_stuck: int = 0
        self._consecutive_api_failures: int = 0
        self._action_count: int = 0
        self._use_simple_model_next: bool = False
        self._client: Any | None = None  # Lazy-initialised Anthropic client
        self._system_prompt: str = self._build_system_prompt()

        # Resolve model IDs from persona ai_routing config
        routing = persona.get("ai_routing", {})
        self._screenshot_model = MODEL_IDS.get(
            routing.get("screenshot_interpretation", "sonnet"),
            MODEL_IDS["sonnet"],
        )
        self._simple_model = MODEL_IDS.get(
            routing.get("simple_actions", "haiku"),
            MODEL_IDS["haiku"],
        )

    def _build_system_prompt(self) -> str:
        """Build the system prompt from persona profile."""
        p = self._persona
        demographics = p.get("demographics", {})
        profile = p.get("profile", "")
        behavior = p.get("behavior_traits", {})

        lines = [
            f"You are {p.get('name', 'Unknown')}, "
            f"a {demographics.get('age', 'unknown')}-year-old "
            f"{demographics.get('role', 'user')}.",
            "",
            profile.strip() if profile else "",
            "",
            f"Tech comfort: {demographics.get('tech_comfort', 'moderate')}",
            f"Patience: {demographics.get('patience', 'medium')}",
            f"You are using a {self._viewport_name} device ({self._viewport_size[0]}x{self._viewport_size[1]}).",
        ]

        # Add behavior modifiers
        if behavior.get("reading_speed") == "slow":
            lines.append("You read carefully and take your time.")
        if behavior.get("explores_ui"):
            lines.append("You like to explore the UI and click around.")
        if behavior.get("adversarial"):
            lines.append("You actively probe for problems and edge cases.")
        if behavior.get("questions_everything"):
            lines.append("You question every UI choice and note confusion points.")

        lines.extend(
            [
                "",
                "IMPORTANT INSTRUCTIONS:",
                "- Look at the screenshot carefully and decide what to do next.",
                "- For click actions, provide approximate x,y pixel coordinates of the target element.",
                "- For fill actions, describe which input field to target (label, placeholder text).",
                "- Report any UX confusion, unclear labels, or accessibility issues in ux_notes.",
                "- Set checkpoint to the name of a checkpoint if you've just reached that state.",
                "- Set goal_achieved to true ONLY when you're confident the goal is complete.",
                "- If you genuinely cannot figure out how to proceed, set action to 'stuck'.",
                "",
                'CRITICAL: When clicking, your "target" field MUST include '
                "the visible text of the button/link/element.",
                "Examples:",
                '  - target: "Create account"',
                '  - target: "Continue to Brief"',
                '  - target: "Next"',
                '  - target: "Ground-Up Construction card"',
                "Do NOT use coordinates alone as your target. "
                "Include the text label so the system can find it reliably.",
                'If you must include coordinates, put them AFTER the text: "Create account at 362, 855"',
                "",
                f"Respond with ONLY valid JSON matching this schema:\n{RESPONSE_SCHEMA}",
            ]
        )

        return "\n".join(lines)

    def _get_client(self) -> Any:
        """Return the cached Anthropic client, creating it lazily on first use."""
        if self._client is None:
            import anthropic

            # Use the API key passed to the constructor, or let the SDK
            # resolve from ANTHROPIC_API_KEY env var.
            kwargs: dict[str, Any] = {"max_retries": 5, "timeout": 60.0}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _should_use_local(self, use_local_model: bool = True) -> bool:
        """Determine if this action should use the local Ollama model.

        Routing rules:
        - Action 1: Always API (high-quality initial assessment + UX observations)
        - Fill actions: Always API (form filling needs careful reasoning)
        - Every 5th action: API (periodic quality checkpoint + UX observation)
        - Near max_actions (goal evaluation): API (need judgment)
        - Otherwise: local model if previous action was simple and succeeded
        """
        if not use_local_model:
            return False  # Scenario/config disabled local model
        if self._action_count == 1:
            return False  # First action always uses API for initial assessment
        if self._action_count % 5 == 0:
            return False  # Periodic quality checkpoint via API
        if not self._use_simple_model_next:
            return False  # Previous action was complex (fill, ux_notes)
        return True

    def _call_ollama(self, screenshot_base64: str, goal: str) -> str | None:
        """Call local Ollama vision model. Returns raw JSON text or None on failure."""
        import urllib.error
        import urllib.request

        payload = json.dumps(
            {
                "model": "llava:13b",
                "messages": [
                    {
                        "role": "system",
                        "content": self._system_prompt,
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Your current goal: {goal}\n\n"
                            f"Action #{self._action_count}. "
                            f"Look at the screenshot and decide your next action.\n\n"
                            f"You MUST respond with ONLY a JSON object. "
                            f"No explanation, no markdown, no text before or after.\n"
                            f'Example: {{"observation":"I see a button","action":"click",'
                            f'"target":"195,420","value":"","reasoning":"clicking the button",'
                            f'"ux_notes":null,"checkpoint":null,"goal_achieved":false}}'
                        ),
                        "images": [screenshot_base64],
                    },
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 512,
                },
            }
        ).encode()

        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("message", {}).get("content", "")
        except Exception as exc:
            logger.warning("Ollama call failed, will fall back to API: %s", exc)
            return None

    def _compact_conversation_history(self) -> None:
        """Replace base64 screenshot data in older messages with text summaries.

        Keeps the most recent ``MAX_SCREENSHOT_HISTORY`` screenshots intact.
        Older user messages that contain an image block have the image replaced
        with a text placeholder that preserves the observation from the
        assistant's response for that turn.
        """
        # Collect indices of user messages that carry a screenshot image block.
        screenshot_indices: list[int] = []
        for idx, msg in enumerate(self._conversation_history):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "image"
                    and isinstance(block.get("source"), dict)
                    and block["source"].get("type") == "base64"
                ):
                    screenshot_indices.append(idx)
                    break  # one screenshot per user message

        if len(screenshot_indices) <= MAX_SCREENSHOT_HISTORY:
            return  # nothing to compact

        # Indices to compact -- everything except the last MAX_SCREENSHOT_HISTORY
        indices_to_compact = screenshot_indices[:-MAX_SCREENSHOT_HISTORY]

        for user_idx in indices_to_compact:
            user_msg = self._conversation_history[user_idx]
            content = user_msg["content"]  # list of blocks

            # Try to find the assistant reply that immediately follows this user
            # message so we can extract the observation summary.
            observation_summary = "no observation recorded"
            assistant_idx = user_idx + 1
            if assistant_idx < len(self._conversation_history):
                assistant_msg = self._conversation_history[assistant_idx]
                if assistant_msg.get("role") == "assistant":
                    assistant_text = assistant_msg.get("content", "")
                    if isinstance(assistant_text, str):
                        try:
                            parsed = json.loads(assistant_text.strip().strip("`").lstrip("json\n"))
                            observation_summary = parsed.get("observation", observation_summary)
                        except (json.JSONDecodeError, AttributeError):
                            # Use a truncated version of the raw text as fallback
                            observation_summary = assistant_text[:120]

            # Determine the action number from the text block in this message.
            action_num = "?"
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    # Pattern: "Action #N."
                    if "Action #" in text:
                        try:
                            action_num = text.split("Action #")[1].split(".")[0]
                        except (IndexError, ValueError):
                            pass
                    break

            # Replace the image block with a text placeholder.
            new_content: list[dict[str, Any]] = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "image"
                    and isinstance(block.get("source"), dict)
                    and block["source"].get("type") == "base64"
                ):
                    new_content.append(
                        {
                            "type": "text",
                            "text": (f"[Screenshot from action {action_num} -- {observation_summary}]"),
                        }
                    )
                else:
                    new_content.append(block)

            user_msg["content"] = new_content

    def decide(
        self,
        goal: str,
        screenshot_base64: str,
        checkpoints: list[dict[str, Any]] | None = None,
        use_local_model: bool = True,
        force_api: bool = False,
        stuck_context: str | None = None,
    ) -> PersonaDecision:
        """Given a goal and screenshot, decide what action to take.

        Args:
            goal: The current goal for the persona to achieve.
            screenshot_base64: Base64-encoded PNG screenshot of the current page.
            checkpoints: Optional list of checkpoint definitions from the scenario.
            use_local_model: Whether to allow local Ollama model for simple actions.
                Set to False to force all-API mode (e.g. critical holdout scenarios).
            force_api: If True, bypass local model routing and always use the API.
                Used by stuck detection to escalate to a stronger model.
            stuck_context: Optional warning message to prepend to the goal when the
                browser runner detects the agent is stuck in a loop.

        Returns:
            PersonaDecision with the action to take.

        Raises:
            AgentStuckError if the agent reports stuck for MAX_CONSECUTIVE_STUCK times.
        """
        self._action_count += 1

        # Build the effective goal -- prepend stuck context if provided
        effective_goal = goal
        if stuck_context:
            effective_goal = f"{stuck_context}\n\n{goal}"

        # Build the user message with screenshot and goal
        checkpoint_text = ""
        if checkpoints:
            cp_names = [cp.get("after", "") for cp in checkpoints]
            checkpoint_text = f"\n\nAvailable checkpoints to report: {', '.join(cp_names)}"

        user_content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_base64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"Your current goal: {effective_goal}\n\n"
                    f"Action #{self._action_count}. "
                    f"Look at the screenshot and decide your next action."
                    f"{checkpoint_text}"
                ),
            },
        ]

        # Add to conversation history and compact old screenshots
        self._conversation_history.append({"role": "user", "content": user_content})
        self._compact_conversation_history()

        # -- Local model routing ----------------------------------------------
        # Try local Ollama model first if routing rules allow it.
        # On success, return early. On failure, fall through to API path.
        # When force_api is True, skip local model entirely -- stuck detection
        # needs the stronger API model to break out of loops.
        if not force_api and self._should_use_local(use_local_model):
            logger.info(
                "Using local model for action %d (previous action was simple)",
                self._action_count,
            )
            ollama_result = self._call_ollama(screenshot_base64, effective_goal)
            if ollama_result is not None:
                decision = self._parse_response(ollama_result)

                # If Ollama returned unparseable prose, fall through to API
                # instead of counting it as "stuck"
                if decision.action == "stuck" and "[Parse error]" in decision.observation:
                    logger.info(
                        "Ollama returned unparseable response for action %d, falling back to API",
                        self._action_count,
                    )
                    # Still track the Ollama call for cost accounting (zero cost)
                    self._cost_tracker.record_call(
                        model="ollama:llava:13b",
                        input_tokens=0,
                        output_tokens=0,
                        purpose="local_navigation_parse_fail",
                    )
                else:
                    # Valid parse -- return the decision
                    # Track as local call (zero API cost)
                    self._cost_tracker.record_call(
                        model="ollama:llava:13b",
                        input_tokens=0,
                        output_tokens=0,
                        purpose="local_navigation",
                    )

                    # Add to conversation history for context continuity
                    self._conversation_history.append({"role": "assistant", "content": ollama_result})

                    # Track stuck state
                    if decision.action == "stuck":
                        self._consecutive_stuck += 1
                        if self._consecutive_stuck >= MAX_CONSECUTIVE_STUCK:
                            raise AgentStuckError(
                                f"Persona agent stuck for {MAX_CONSECUTIVE_STUCK} "
                                f"consecutive actions. "
                                f"Last observation: {decision.observation}"
                            )
                    else:
                        self._consecutive_stuck = 0

                    # Route next action: simple actions stay local, complex go to API.
                    # UX notes don't affect routing -- those are a bonus from API calls.
                    if decision.action in SIMPLE_ACTIONS:
                        self._use_simple_model_next = True
                    else:
                        self._use_simple_model_next = False

                    return decision
            else:
                logger.info(
                    "Ollama fallback -- using Sonnet API for action %d",
                    self._action_count,
                )
        else:
            # Log the reason for using API
            if force_api:
                reason = "forced by stuck detection"
            elif self._action_count == 1:
                reason = "first action (initial assessment)"
            elif self._action_count % 5 == 0:
                reason = "periodic quality checkpoint"
            elif not self._use_simple_model_next:
                reason = "previous action was complex"
            else:
                reason = "local model disabled"
            logger.info(
                "Using API for action %d (%s)",
                self._action_count,
                reason,
            )

        # -- Anthropic API path -----------------------------------------------
        # Select model: use _simple_model when the previous action was simple.
        # When force_api is True (stuck detection), always use the stronger
        # screenshot model -- the agent needs maximum reasoning to break out.
        if force_api:
            model = self._screenshot_model
        else:
            model = self._simple_model if self._use_simple_model_next else self._screenshot_model
        # Reset -- will be re-evaluated after we parse the response
        self._use_simple_model_next = False

        # Make API call (client is cached across calls)
        client = self._get_client()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=self._system_prompt,
                messages=self._conversation_history,
            )
        except Exception as exc:
            logger.error("Anthropic API call failed: %s", exc)
            # Track API failures separately from agent stuckness.
            # "API failure" = the AI never got to see the screen
            # "stuck" = the AI looked at the screen and didn't know what to do
            self._consecutive_api_failures += 1
            if self._consecutive_api_failures >= MAX_CONSECUTIVE_API_FAILURES:
                raise AgentStuckError(
                    f"Persona agent experienced {MAX_CONSECUTIVE_API_FAILURES} consecutive API failures. "
                    f"Last error: {exc}"
                )
            return PersonaDecision(
                observation=f"API error: {exc}",
                action="wait",
                target="",
                value="",
                reasoning=(
                    f"Anthropic API call failed "
                    f"(attempt {self._consecutive_api_failures}/{MAX_CONSECUTIVE_API_FAILURES}). "
                    f"Retrying..."
                ),
                ux_notes=None,
                checkpoint=None,
                goal_achieved=False,
            )

        # Reset API failure counter on successful call
        self._consecutive_api_failures = 0

        # Track costs
        usage = response.usage
        self._cost_tracker.record_call(
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            purpose="screenshot_interpretation",
        )

        # Parse response
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        decision = self._parse_response(raw_text)

        # Add assistant response to conversation history
        self._conversation_history.append({"role": "assistant", "content": raw_text})

        # Determine whether the NEXT call can use the cheaper simple model.
        # Route based on action type only: simple actions use local model,
        # complex actions (fill) require the full API model.
        # UX notes don't affect routing -- those are a bonus from API calls.
        if decision.action in SIMPLE_ACTIONS:
            self._use_simple_model_next = True
        else:
            self._use_simple_model_next = False

        # Track stuck state
        if decision.action == "stuck":
            self._consecutive_stuck += 1
            if self._consecutive_stuck >= MAX_CONSECUTIVE_STUCK:
                raise AgentStuckError(
                    f"Persona agent stuck for {MAX_CONSECUTIVE_STUCK} consecutive actions. "
                    f"Last observation: {decision.observation}"
                )
        else:
            self._consecutive_stuck = 0

        return decision

    def reset_history(self) -> None:
        """Clear conversation history (e.g., between browser steps)."""
        self._conversation_history.clear()
        self._consecutive_stuck = 0
        self._consecutive_api_failures = 0
        self._action_count = 0
        self._use_simple_model_next = False

    @property
    def action_count(self) -> int:
        return self._action_count

    @staticmethod
    def _try_extract_json(raw_text: str) -> str | None:
        """Try to extract a JSON object from text that may contain prose around it."""
        import re

        # Look for a JSON-like block in the response
        match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', raw_text, re.DOTALL)
        if match:
            return match.group(0)
        return None

    @staticmethod
    def _parse_response(raw_text: str) -> PersonaDecision:
        """Parse the JSON response from the AI model.

        Handles cases where the model wraps JSON in markdown code blocks.
        Falls back to a stuck decision if parsing fails.
        """
        text = raw_text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Strip the opening fence (first line)
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            # Strip the closing fence (last line)
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            return PersonaDecision.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # Try to extract JSON from prose response (common with local models)
            extracted = PersonaAgent._try_extract_json(raw_text)
            if extracted:
                try:
                    data = json.loads(extracted)
                    logger.info("Extracted JSON from prose response")
                    return PersonaDecision.from_dict(data)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            logger.warning("Failed to parse persona response: %s\nRaw: %s", exc, raw_text[:500])
            return PersonaDecision(
                observation=f"[Parse error] Raw response: {raw_text[:200]}",
                action="stuck",
                target="",
                value="",
                reasoning=f"Could not parse AI response as JSON: {exc}",
                ux_notes=None,
                checkpoint=None,
                goal_achieved=False,
            )


class AgentStuckError(Exception):
    """Raised when a persona agent is stuck for too many consecutive actions."""

    pass
