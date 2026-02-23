# GhostQA for AI Agents

This document covers how AI agents and automated systems can use GhostQA programmatically. If you're building agent tooling, orchestrating test runs from code, or planning to integrate GhostQA into an agent workflow, this is for you.

## Overview

GhostQA provides three integration surfaces:

1. **CLI with JSON output** -- Simplest. Shell out to `ghostqa run` with `--output json`.
2. **Python API** -- Import and invoke directly. Full control over config and execution.
3. **Federated Protocol** -- Swap in your own AI decider or action executor. Use GhostQA's loop with your own brain.
4. **MCP Server** -- Shipped. Model Context Protocol server for agent-native tool discovery and invocation.

## CLI Integration

The fastest way for an agent to use GhostQA:

```bash
ghostqa run -p myapp --output json --level smoke --budget 2.00
```

- Human-readable progress goes to **stderr**
- Structured JSON results go to **stdout**
- Exit code tells you pass/fail without parsing

### JSON Output Schema

```json
{
  "run_id": "GQA-RUN-20260222-143052-a1b2",
  "passed": true,
  "scenario_name": "Onboarding Happy Path",
  "scenario_id": "onboarding-happy-path",
  "product_name": "myapp",
  "persona_name": "Alex Chen",
  "persona_role": "Full-Stack Developer",
  "viewport_name": "desktop",
  "viewport_size": [1280, 720],
  "start_time": "2026-02-22T14:30:52+00:00",
  "end_time": "2026-02-22T14:31:37+00:00",
  "duration_seconds": 45.2,
  "cost_usd": 0.4521,
  "step_reports": [
    {
      "step_id": "visit_homepage",
      "description": "Navigate to the homepage",
      "mode": "browser",
      "passed": true,
      "duration_seconds": 12.3,
      "error": null
    },
    {
      "step_id": "fill_signup_form",
      "description": "Complete the signup form",
      "mode": "browser",
      "passed": false,
      "duration_seconds": 28.1,
      "error": "Max actions (30) reached without achieving goal"
    }
  ],
  "findings": [
    {
      "severity": "high",
      "category": "ux",
      "description": "Submit button is disabled but no validation error explains why",
      "evidence": "",
      "step_id": "fill_signup_form"
    }
  ],
  "cost_summary": {
    "total_cost_usd": 0.4521,
    "total_input_tokens": 125000,
    "total_output_tokens": 8500,
    "calls_by_model": {
      "claude-haiku-4-5-20251001": 15,
      "claude-sonnet-4-20250514": 8
    },
    "cost_by_model": {
      "claude-haiku-4-5-20251001": 0.134,
      "claude-sonnet-4-20250514": 0.318
    },
    "budget_limit_usd": 5.0,
    "budget_remaining_usd": 4.5479,
    "budget_exceeded": false,
    "call_count": 23
  }
}
```

### Key Fields for Agent Consumers

| Field | Why It Matters |
|-------|---------------|
| `passed` | Top-level pass/fail boolean. Check this first. |
| `findings` | Actionable issues found during the run. Each has a severity. |
| `cost_usd` | How much this run cost. Track this to stay within budget. |
| `step_reports[].error` | If a step failed, this tells you why. |
| `cost_summary.budget_remaining_usd` | How much budget was left. Useful for deciding whether to run more tests. |

## Python API

For tighter integration, use GhostQA as a Python library.

### Basic Usage

```python
from pathlib import Path
from ghostqa.config import GhostQAConfig
from ghostqa.engine.orchestrator import GhostQAOrchestrator

# Configure
config = GhostQAConfig()
config.project_dir = Path(".ghostqa")
config.products_dir = Path(".ghostqa/products")
config.personas_dir = Path(".ghostqa/personas")
config.journeys_dir = Path(".ghostqa/journeys")
config.evidence_dir = Path(".ghostqa/evidence")
config.anthropic_api_key = "sk-ant-..."
config.budget = 5.00
config.headless = True

# Run
orchestrator = GhostQAOrchestrator(config)
report_md, all_passed = orchestrator.run(
    product="myapp",
    level="smoke",
)

# report_md is a markdown string with findings
# all_passed is True if all steps passed and no blocking findings
```

### Loading Config from File

```python
config = GhostQAConfig.from_file(Path(".ghostqa/config.yaml"))
config.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
config.product_name = "myapp"
```

### Reading Run Results Programmatically

After a run, structured results are saved to the evidence directory:

```python
import json
from pathlib import Path

evidence_dir = Path(".ghostqa/evidence")
# Find most recent run
run_dirs = sorted(evidence_dir.glob("GQA-RUN-*"), reverse=True)
if run_dirs:
    result_file = run_dirs[0] / "run-result.json"
    result = json.loads(result_file.read_text())

    print(f"Passed: {result['passed']}")
    print(f"Cost: ${result['cost_usd']:.4f}")
    for finding in result.get("findings", []):
        print(f"  [{finding['severity']}] {finding['description']}")
```

### Cost Tracking

The `CostTracker` class provides detailed cost information:

```python
from ghostqa.engine.cost_tracker import CostTracker

# Check cumulative budget before starting a run
budget_status = CostTracker.check_cumulative_budget(
    base_dir=Path(".ghostqa"),
    per_day_usd=20.00,
    per_month_usd=200.00,
)

if not budget_status["daily_ok"]:
    print(f"Daily budget exceeded: ${budget_status['daily_spent']:.2f}")
    # Don't start the run

if not budget_status["monthly_ok"]:
    print(f"Monthly budget exceeded: ${budget_status['monthly_spent']:.2f}")
```

## Federated Protocol

GhostQA's engine is modular. The core `AIStepRunner` accepts any implementation of the `AIDecider` and `ActionExecutor` protocols. This means you can:

- Use your own AI model (GPT, Gemini, local LLM) as the decision-maker
- Use your own action execution backend (custom browser automation, hardware control, etc.)
- Use GhostQA's stuck detection, evidence collection, and cost tracking with your own components

### The Protocols

```python
from ghostqa.engine.protocols import AIDecider, ActionExecutor, Decision, ActionResult

# AIDecider: receives screenshot + goal, returns action decision
class AIDecider(Protocol):
    def decide(
        self,
        goal: str,
        screenshot_base64: str,
        ui_context: str = "",
        force_api: bool = False,
        stuck_context: str | None = None,
        **kwargs,
    ) -> Decision: ...

# ActionExecutor: translates decisions into platform actions
class ActionExecutor(Protocol):
    def execute(self, decision: Decision) -> ActionResult: ...
```

### The Decision Dataclass

```python
@dataclasses.dataclass
class Decision:
    action: str        # click | fill | keyboard | scroll | wait | done | stuck
    target: str        # Element label or description
    value: str         # Text to type, key name, seconds to wait
    reasoning: str     # Why this action was chosen
    goal_achieved: bool
    observation: str = ""
    ux_notes: str | None = None
    checkpoint: str | None = None
```

### Custom Decider Example

```python
from ghostqa.engine.protocols import Decision

class MyGPTDecider:
    """Use GPT-4 Vision as the AI brain instead of Claude."""

    def __init__(self, api_key: str):
        self._client = openai.OpenAI(api_key=api_key)

    def decide(
        self,
        goal: str,
        screenshot_base64: str,
        ui_context: str = "",
        force_api: bool = False,
        stuck_context: str | None = None,
        **kwargs,
    ) -> Decision:
        response = self._client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": f"Goal: {goal}"},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{screenshot_base64}"
                    }}
                ]}
            ],
        )
        # Parse response into Decision...
        return Decision(...)
```

### Using AIStepRunner

```python
from ghostqa.engine.ai_step_runner import AIStepRunner

runner = AIStepRunner(
    screenshot_fn=my_screenshot_fn,   # (step_id, action_idx, label) -> filepath
    decider=my_decider,               # Implements AIDecider protocol
    executor=my_executor,             # Implements ActionExecutor protocol
    evidence_dir=Path("/tmp/evidence"),
)

step = {
    "id": "login",
    "goal": "Log in with test credentials",
    "max_actions": 20,
    "max_duration_seconds": 120,
}

result = runner.execute_step(step)
print(f"Passed: {result.passed}")
print(f"Actions taken: {result.action_count}")
print(f"UX observations: {result.ux_observations}")
```

## MCP Server

GhostQA ships a native MCP (Model Context Protocol) server. Any MCP-compatible agent host (Claude Desktop, Cursor, Cline, or a custom agent using the MCP SDK) can discover and invoke GhostQA as a structured tool without shelling out to the CLI.

### Setup

Add GhostQA to your MCP client config:

```json
{
  "ghostqa": {
    "command": "ghostqa-mcp",
    "args": []
  }
}
```

Alternatively, run it directly:

```bash
ghostqa-mcp
# or
python -m ghostqa.mcp
```

Both use stdio transport (stdin/stdout). No port configuration required.

### Available Tools

| Tool | Description |
|------|-------------|
| `ghostqa_run` | Execute behavioral tests against a product. Synchronous — may take 45-300 seconds. Incurs Anthropic API costs (default budget $5.00). |
| `ghostqa_list_products` | List configured products and available journeys. |
| `ghostqa_get_results` | Read full structured results from a completed run by run ID. |
| `ghostqa_init` | Initialize a new GhostQA project directory with sample configs. |

### ghostqa_run Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `product` | string | Yes | Product name (matches filename in `.ghostqa/products/`) |
| `journey` | string | No | Journey ID to run. Runs all journeys if omitted. |
| `level` | string | No | `smoke`, `standard`, or `thorough` |
| `budget` | float | No | Cost cap in USD (default: 5.00) |
| `directory` | string | No | Project root directory. Defaults to CWD. |

### ghostqa_run Response

The MCP tool response uses the same JSON schema as `--output json`. Key fields:

```json
{
  "passed": true,
  "run_id": "GQA-RUN-20260222-143052-a1b2",
  "step_reports": [...],
  "findings": [],
  "cost_usd": 0.4521,
  "cost_summary": {
    "budget_limit_usd": 5.0,
    "budget_remaining_usd": 4.5479,
    "budget_exceeded": false
  }
}
```

### Agent Workflow with MCP

Agents can use GhostQA MCP tools to verify generated code:

```
1. Agent writes application code
2. Agent starts dev server (e.g., npm run dev)
3. Agent calls ghostqa_list_products to confirm product config exists
4. Agent calls ghostqa_run with product name and budget cap
5. Agent reads findings from the response
6. If tests fail: agent fixes issues and loops back to step 4
7. If tests pass: proceed to commit
```

This enables fully autonomous quality verification loops without human intervention.

## Integration Patterns

### Agent-Driven QA Loop

An agent building a feature can use GhostQA to verify its work:

```
1. Agent writes code
2. Agent starts dev server
3. Agent runs: ghostqa run -p myapp --output json --level smoke
4. If tests fail:
   a. Agent reads findings from JSON
   b. Agent fixes the issues
   c. Go to step 3
5. If tests pass: commit and move on
```

### Scheduled Quality Checks

Run GhostQA on a schedule to catch regressions:

```python
import subprocess
import json

result = subprocess.run(
    ["ghostqa", "run", "-p", "myapp", "--output", "json", "--budget", "5.00"],
    capture_output=True,
    text=True,
)

data = json.loads(result.stdout)
if not data["passed"]:
    # Alert, create issue, notify team
    findings = data["findings"]
    for f in findings:
        if f["severity"] in ("block", "critical", "high"):
            create_alert(f"GhostQA: [{f['severity']}] {f['description']}")
```

### Pre-Deploy Gate

Use GhostQA as a deployment gate:

```bash
ghostqa run -p myapp --level standard --budget 10.00 --junit-xml results.xml
if [ $? -ne 0 ]; then
    echo "GhostQA behavioral tests failed. Blocking deploy."
    exit 1
fi
# Proceed with deployment
```

## Error Handling

When integrating programmatically, handle these exceptions:

| Exception | When | What to Do |
|-----------|------|------------|
| `GhostQAConfigError` | Bad config, missing files | Fix config, check file paths |
| `BudgetExceededError` | Run cost hit the cap | Increase budget or reduce test scope |
| `CumulativeBudgetExceededError` | Daily/monthly cap hit | Wait for next period or increase caps |
| `ImportError` | Playwright not installed | Run `ghostqa install` |
| `AgentStuckError` | AI couldn't figure out what to do | Check app state, simplify the goal |

All of these are catchable Python exceptions from the `ghostqa` package.
