# SpecterQA

[![PyPI version](https://img.shields.io/pypi/v/specterqa.svg)](https://pypi.org/project/specterqa/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/SyncTek-LLC/specterqa/workflows/CI/badge.svg)](https://github.com/SyncTek-LLC/specterqa/actions)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-blueviolet)](https://github.com/SyncTek-LLC/specterqa/blob/main/docs/for-agents.md)

**AI personas walk your app so real users don't trip.**

SpecterQA sends AI personas through your application — they look at the screen, decide what to do, and interact like real humans. No test scripts. No selectors. You describe personas and journeys in YAML, and SpecterQA handles the rest.

```
$ specterqa run -p myapp

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ SpecterQA Run                                                    ┃
┃ Product: myapp   Budget: $5.00   Viewport: 1280x720            ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

  ✓ Step 1/4: Navigate to homepage       PASS   3.2s   $0.0081
  ✓ Step 2/4: Click signup link          PASS   2.1s   $0.0043
  ✓ Step 3/4: Fill registration form     PASS   8.7s   $0.0312
  ✓ Step 4/4: Verify dashboard loads     PASS   4.5s   $0.0127

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ALL TESTS PASSED                                                ┃
┃ Steps: 4/4   Findings: 0   Duration: 18.5s   Cost: $0.0563     ┃
┃ Run ID: GQA-RUN-20260222-143052-a1b2                            ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

---

## What is this?

Traditional E2E tests are brittle. You write selectors, they break. You maintain scripts, they rot. SpecterQA takes a different approach: AI vision models look at your actual UI and navigate it the way a person would.

You define **personas** (who is using your app) and **journeys** (what they're trying to do). SpecterQA's engine takes a screenshot, sends it to a Claude vision model, gets back a decision ("click this button", "fill this field"), executes it via Playwright, takes another screenshot, and repeats until the goal is achieved or something goes wrong.

When something goes wrong, you get evidence: screenshots, UX observations, cost breakdowns, and findings categorized by severity.

## Quick Start

```bash
pip install specterqa
specterqa install          # downloads Playwright browsers
specterqa init             # scaffolds .specterqa/ with sample configs
specterqa run -p demo      # runs the sample journey
```

You'll need an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

That's it. Three commands and an API key.

## How It Works

The core loop is simple:

```
screenshot --> vision model --> action decision --> execute --> repeat
```

1. **Screenshot** -- Playwright captures the current page state as a PNG
2. **Decide** -- A Claude vision model receives the screenshot + persona context + goal, returns a structured JSON action (`click`, `fill`, `navigate`, `scroll`, `keyboard`, `wait`, `done`, or `stuck`)
3. **Execute** -- Playwright performs the action (click at coordinates, type text, navigate to URL, etc.)
4. **Repeat** -- Loop until the goal is achieved, the agent gets stuck, or the budget runs out

The persona's profile shapes how the AI behaves. A "tech-savvy developer" explores differently than a "frustrated first-time user." Persona patience, tech comfort, and frustrations all influence the system prompt.

**Model routing** keeps costs down. Simple actions (click, scroll) use Haiku. Complex actions (form filling, initial assessment) use Sonnet. You can also route simple actions to a local Ollama model (llava:13b) for zero API cost on straightforward navigation.

## Features

- **Persona-based testing** -- Define AI users with backgrounds, goals, frustrations, and tech comfort levels. They don't just follow scripts; they react to what they see.
- **Vision-powered** -- No selectors, no DOM queries. The AI interprets screenshots like a human would. Catches visual/layout issues that selector-based tests miss entirely.
- **YAML-configured** -- Products, personas, and journeys are all YAML files. PMs can read them. No code to maintain.
- **Budget enforcement** -- Per-run, per-day, and per-month cost caps. The engine hard-stops if you hit the limit. No surprise bills.
- **JUnit XML output** -- Drop `--junit-xml results.xml` and plug it into any CI system.
- **Tiered model routing** -- Haiku for cheap navigation, Sonnet for complex reasoning, optional local Ollama for zero-cost simple actions.
- **Multi-platform** -- Web apps (via Playwright), macOS native apps (via Accessibility API + pyobjc), iOS Simulator (via simctl). Same YAML format, different runners.
- **Evidence collection** -- Every run produces screenshots, a findings report, cost breakdown, and a structured JSON result. Everything is saved to an evidence directory.
- **Stuck detection** -- If the AI repeats the same action or the UI stops changing, the engine escalates to a stronger model, then aborts if nothing works. No infinite loops.
- **Template variables** -- Use `{{persona.credentials.email}}` in your journey steps. Variables resolve from persona configs at runtime.
- **Precondition checks** -- Verify services are up before running tests. Fail fast with clear errors instead of wasting API calls.

## Configuration

SpecterQA uses three types of YAML config files, all living in `.specterqa/`:

### Product (`products/myapp.yaml`)

```yaml
product:
  name: myapp
  display_name: "My Application"
  base_url: "http://localhost:3000"

  services:
    frontend:
      url: "http://localhost:3000"
      health_endpoint: /

  viewports:
    desktop:
      width: 1280
      height: 720
    mobile:
      width: 375
      height: 812

  cost_limits:
    per_run_usd: 5.00
```

### Persona (`personas/alex-developer.yaml`)

```yaml
persona:
  name: alex_developer
  display_name: "Alex Chen"
  role: "Full-Stack Developer"
  age: 28
  tech_comfort: high
  patience: medium
  preferred_device: desktop

  goals:
    - "Evaluate the app from a developer's perspective"
    - "Check for common UX anti-patterns"

  frustrations:
    - "Unclear error messages"
    - "Missing loading indicators"

  credentials:
    email: "alex@example.com"
    password: "TestPass123!"
```

### Journey (`journeys/onboarding.yaml`)

```yaml
scenario:
  id: onboarding-happy-path
  name: "Onboarding Happy Path"
  description: "New user signs up, completes onboarding, reaches dashboard."
  tags: [onboarding, critical_path, smoke]

  personas:
    - ref: alex_developer
      role: primary

  preconditions:
    - service: frontend
      check: /
      expected_status: 200

  steps:
    - id: visit_homepage
      mode: browser
      goal: "Navigate to the homepage and verify it loads"
      checkpoints:
        - type: text_present
          value: "Welcome"

    - id: navigate_signup
      mode: browser
      goal: "Find and click the signup link"

    - id: fill_signup_form
      mode: browser
      goal: "Complete the signup form with test credentials"

    - id: verify_dashboard
      mode: browser
      goal: "Verify signup succeeded and the dashboard loads"
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

## CI Integration

SpecterQA is built for CI. It runs headless by default and returns proper exit codes.

```bash
# Basic CI run
specterqa run -p myapp --junit-xml results.xml

# Smoke test (runs first scenario only, fast)
specterqa run -p myapp --level smoke --budget 2.00

# JSON output for programmatic consumption
specterqa run -p myapp --output json > results.json
```

**Exit codes:**
- `0` -- all tests passed
- `1` -- one or more tests failed
- `2` -- configuration error
- `3` -- infrastructure error (missing dependencies, API unreachable)

See [docs/ci-integration.md](docs/ci-integration.md) for GitHub Actions, GitLab CI, and CircleCI examples.

## Cost

SpecterQA uses Anthropic's Claude API. Every run costs money. Here's what to expect:

| Model | Role | Input (per 1M tokens) | Output (per 1M tokens) |
|-------|------|-----------------------|------------------------|
| Claude Haiku 4.5 | Simple navigation | $0.80 | $4.00 |
| Claude Sonnet 4 | Complex reasoning | $3.00 | $15.00 |
| Ollama llava:13b | Local fallback | Free | Free |

**Typical costs per run:**
- 3-step smoke test: ~$0.30-0.60
- 5-step standard journey: ~$0.50-1.50
- Complex 10-step journey with forms: ~$1.00-3.00

The default budget is **$5.00 per run**. The engine hard-stops if the budget is exceeded -- no silent overruns. You can set per-day and per-month caps too.

You can also set a default budget via an environment variable to avoid passing `--budget` every time:

```bash
export SPECTERQA_BUDGET=2.00
specterqa run -p myapp          # uses $2.00 budget
specterqa run -p myapp --budget 5.00  # uses $5.00 budget (CLI flag wins)
```

Model routing helps: simple clicks and scrolls use Haiku (~$0.01 per action), while form fills and initial assessments use Sonnet (~$0.03-0.05 per action). If you have a local Ollama instance, simple actions can route there for zero API cost.

See [docs/cost-guide.md](docs/cost-guide.md) for detailed cost breakdowns and budgeting strategies.

## Multi-Platform

SpecterQA isn't web-only. The same persona/journey YAML format works across platforms:

**Web apps** (default) -- Uses Playwright for browser automation.

**macOS native apps** -- Uses the macOS Accessibility API via pyobjc. The AI reads the accessibility tree and screenshots, then executes clicks and keypresses through AX actions.

```yaml
product:
  name: my-mac-app
  app_type: native_macos
  app_path: /Applications/MyApp.app
  bundle_id: com.example.myapp
```

**iOS Simulator** -- Uses `simctl` for screenshots and touch simulation. Useful for testing iOS apps without a physical device.

```yaml
product:
  name: my-ios-app
  app_type: ios_simulator
  bundle_id: com.example.myiosapp
  simulator_device: "iPhone 15 Pro"
  simulator_os: "17.2"
```

Native and simulator support require the `native` optional dependency:

```bash
pip install specterqa[native]
```

## For AI Agents

If you're an AI agent or building agent tooling, SpecterQA provides structured interfaces for programmatic use.

### CLI with JSON output

```bash
specterqa run -p myapp --output json
```

Returns structured JSON to stdout:

```json
{
  "passed": true,
  "run_id": "GQA-RUN-20260222-143052-a1b2",
  "step_reports": [
    {
      "step_id": "visit_homepage",
      "passed": true,
      "duration_seconds": 12.3
    }
  ],
  "findings": [],
  "cost_usd": 0.4521
}
```

### Python API

```python
from specterqa.config import SpecterQAConfig
from specterqa.engine.orchestrator import SpecterQAOrchestrator

config = SpecterQAConfig()
config.project_dir = Path(".specterqa")
config.products_dir = Path(".specterqa/products")
config.personas_dir = Path(".specterqa/personas")
config.journeys_dir = Path(".specterqa/journeys")
config.evidence_dir = Path(".specterqa/evidence")
config.anthropic_api_key = "sk-ant-..."
config.budget = 5.00
config.headless = True

orchestrator = SpecterQAOrchestrator(config)
report_md, all_passed = orchestrator.run(product="myapp", level="smoke")
```

### Federated Protocol

SpecterQA exposes a `protocols.py` module with Python Protocol classes (`AIDecider`, `ActionExecutor`) that let you swap in your own AI model or action backend:

```python
from specterqa.engine.protocols import AIDecider, Decision

class MyCustomDecider:
    def decide(self, goal, screenshot_base64, **kwargs) -> Decision:
        # Your logic here
        ...
```

### MCP Server

SpecterQA ships an MCP (Model Context Protocol) server. Any MCP-compatible agent (Claude Desktop, Cursor, Cline, custom agent tooling) can discover and invoke SpecterQA as a tool -- run tests, read results, manage configs -- without shelling out to the CLI.

**Add to your MCP client config (`claude_desktop_config.json` or equivalent):**

```json
{
  "specterqa": {
    "command": "specterqa-mcp",
    "args": []
  }
}
```

**Available tools:**

| Tool | Description |
|------|-------------|
| `specterqa_run` | Execute behavioral tests against a product. Synchronous — may take 45-300s. Incurs API costs (default budget: $5.00). |
| `specterqa_list_products` | List configured products and their available journeys |
| `specterqa_get_results` | Retrieve full structured results from a previous run by run ID |
| `specterqa_init` | Initialize a new SpecterQA project directory |

See [docs/for-agents.md](docs/for-agents.md) for the full programmatic API reference and MCP integration details.

## Security

**Directory access:** When the environment variable `SPECTERQA_ALLOWED_DIRS` is unset, the SpecterQA MCP server permits the `directory` parameter of `specterqa_run` to point at **any path on the filesystem** accessible to the process. In shared or multi-user environments — or anywhere the MCP server is exposed to untrusted agents — you should set this variable to an explicit allowlist:

```bash
export SPECTERQA_ALLOWED_DIRS="/home/user/projects:/ci/workspaces"
```

When set, the MCP server rejects any `directory` value that is not under one of the listed prefixes. This mitigates the MCP directory traversal vector described in [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) (GHSA-SPECTERQA-001).

**Command injection fix (v0.2.1):** The `check_command` field in product YAML service definitions has been removed. It was the source of a critical command injection vulnerability. Precondition checks are now limited to TCP connectivity and HTTP health endpoint checks, which are safe. See [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) for full details.

**Credential scrubbing:** Run artifacts (JSON result files, log output) automatically scrub known credential patterns — API keys, tokens, passwords — from captured content before writing to disk.

**Reporting vulnerabilities:** Do not open public issues for security bugs. Email **info@synctek.io** or see [SECURITY.md](SECURITY.md) for the full disclosure policy.

## Limitations

Be honest with yourself about what this is and isn't:

- **Requires an Anthropic API key.** No API key, no testing. There's no free tier built into SpecterQA itself.
- **Costs money.** Every run makes API calls. A typical 3-step journey costs $0.30-0.60. Budget enforcement prevents surprises, but the meter is always running.
- **Vision models aren't perfect.** The AI sometimes misreads small text, clicks the wrong element, or gets confused by complex layouts. It's good, not infallible. You'll occasionally see false positives and false negatives.
- **Not a replacement for unit tests.** SpecterQA tests behavioral UX flows. It doesn't test your business logic, data integrity, or edge case handling. Use it alongside your existing test suite, not instead of it.
- **macOS native testing requires pyobjc.** The `specterqa[native]` extra pulls in pyobjc packages (~200MB). Only needed for native macOS and iOS Simulator testing.
- **Alpha software.** Version 0.3.0. APIs may change. File structure may change. Expect rough edges.
- **Single-persona per journey (for now).** Multi-persona concurrent testing (e.g., simulating a chat between two users) is on the roadmap but not yet supported.
- **Deterministic reproduction is hard.** Because the AI makes decisions at runtime, the exact sequence of actions varies between runs. Same journey, same persona, slightly different clicks. This is by design (it catches more issues) but makes exact reproduction tricky.

## Contributing

Contributions welcome. The repo is at [github.com/SyncTek-LLC/specterqa](https://github.com/SyncTek-LLC/specterqa).

```bash
git clone https://github.com/SyncTek-LLC/specterqa.git
cd specterqa
pip install -e ".[dev]"
pytest
```

Open an issue before starting large PRs. We'd rather discuss the approach first.

## License

MIT -- see [LICENSE](LICENSE) for details.

---

<!-- mcp-name: io.github.SyncTekLLC/specterqa -->

Built by [SyncTek LLC](https://github.com/SyncTek-LLC).
