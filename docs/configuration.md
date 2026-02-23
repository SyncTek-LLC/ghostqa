# Configuration Reference

SpecterQA uses YAML files organized under a `.specterqa/` project directory. This document covers every configuration option.

## Project Structure

```
.specterqa/
  config.yaml           # Global project settings
  products/
    myapp.yaml          # Product definition
  personas/
    alex-developer.yaml # Persona definition
  journeys/
    onboarding.yaml     # Journey/scenario definition
  evidence/
    GQA-RUN-*/          # Run artifacts (auto-generated)
```

Run `specterqa init` to scaffold this structure with sample files.

## Global Config (`config.yaml`)

The project-level config lives at `.specterqa/config.yaml`. All fields are optional -- CLI flags and defaults fill in the gaps.

```yaml
# Default budget per run (USD). Overridden by --budget flag.
budget: 5.00

# Run browser in headless mode by default. Override with --no-headless.
headless: true

# Default viewport for browser steps.
viewport:
  width: 1280
  height: 720

# Global run timeout in seconds. The engine aborts if a run exceeds this.
timeout: 600

# API key (optional -- env var ANTHROPIC_API_KEY takes priority).
# Uncomment if you prefer to store it in config rather than env.
# anthropic_api_key: sk-ant-...

# Override default directories (relative to .specterqa/).
# products_dir: products
# personas_dir: personas
# journeys_dir: journeys
# evidence_dir: evidence
```

### API Key Resolution Order

1. `ANTHROPIC_API_KEY` environment variable
2. `anthropic_api_key` in `.specterqa/config.yaml`
3. `.env` file in project root (loaded via python-dotenv)

## Product Config

Products define what you're testing -- the app URL, available services, viewports, and cost limits.

**File location:** `.specterqa/products/{name}.yaml`

```yaml
product:
  name: myapp                          # Slug used in CLI: --product myapp
  display_name: "My Application"       # Human-readable name for reports

  # Entry point URL
  base_url: "http://localhost:3000"

  # Services that need to be running before tests start
  services:
    frontend:
      url: "http://localhost:3000"
      health_endpoint: /               # GET this endpoint, expect 200
    backend:
      url: "http://localhost:3001"
      health_endpoint: /api/health

  # Named viewports for responsive testing
  viewports:
    desktop:
      width: 1280
      height: 720
    tablet:
      width: 768
      height: 1024
    mobile:
      width: 375
      height: 812

  # Cost caps for this product
  cost_limits:
    per_run_usd: 5.00                  # Hard stop per run
    warn_at_pct: 80                    # Warn at 80% of budget
```

### Native macOS App

```yaml
product:
  name: my-mac-app
  app_type: native_macos               # Switches to native app runner
  app_path: /Applications/MyApp.app    # Path to .app bundle
  bundle_id: com.example.myapp         # Optional, used for activation
  timeout: 300

  cost_limits:
    per_run_usd: 2.00
```

### iOS Simulator App

```yaml
product:
  name: my-ios-app
  app_type: ios_simulator              # Switches to simulator runner
  bundle_id: com.example.myiosapp      # Required for simctl
  app_path: /path/to/build/MyApp.app   # Optional, for auto-install
  simulator_device: "iPhone 15 Pro"    # Device name or UDID
  simulator_os: "17.2"                 # iOS version

  cost_limits:
    per_run_usd: 3.00
```

## Persona Config

Personas define who is using your app. The AI uses the persona profile to shape its behavior -- what it looks at, how patient it is, and what kind of issues it notices.

**File location:** `.specterqa/personas/{name}.yaml`

```yaml
persona:
  name: alex_developer                 # Reference ID used in journeys
  display_name: "Alex Chen"
  role: "Full-Stack Developer"
  age: 28
  tech_comfort: high                   # low | medium | high
  patience: medium                     # low | medium | high
  preferred_device: desktop            # Matches a viewport name in product config

  # What this persona is trying to accomplish
  goals:
    - "Evaluate the app from a developer's perspective"
    - "Check for common UX anti-patterns"
    - "Verify forms work correctly"

  # What frustrates this persona (the AI will notice these patterns)
  frustrations:
    - "Unclear error messages"
    - "Slow page loads"
    - "Missing loading indicators"

  # Test credentials (referenced via {{persona.credentials.email}} in journeys)
  credentials:
    email: "alex@example.com"
    password: "TestPass123!"

  # Behavior modifiers (optional)
  behavior_traits:
    reading_speed: fast                # slow | normal | fast
    explores_ui: false                 # If true, persona clicks around more
    adversarial: false                 # If true, persona probes for edge cases
    questions_everything: false        # If true, notes every UX confusion point

  # AI model routing (optional -- defaults are sensible)
  ai_routing:
    screenshot_interpretation: sonnet  # haiku | sonnet | opus
    simple_actions: haiku              # haiku | sonnet
```

### Persona Design Tips

- **Tech comfort** affects how the AI navigates. `low` = more confused by jargon, looks for help text. `high` = tries keyboard shortcuts, reads error codes.
- **Patience** affects how quickly the AI gives up or reports frustration. `low` = reports issues after one failed attempt. `high` = retries multiple times.
- **Adversarial personas** are useful for security/edge-case testing. They'll try empty inputs, special characters, and unusual navigation paths.
- **Frustrations** prime the AI to notice specific UX patterns. If "unclear error messages" is listed, the AI is more likely to flag vague validation errors.

## Journey Config

Journeys (also called scenarios) define what the persona does -- a sequence of goal-oriented steps.

**File location:** `.specterqa/journeys/{name}.yaml`

```yaml
scenario:
  id: onboarding-happy-path            # Unique scenario ID
  name: "Onboarding Happy Path"
  description: "New user signs up, completes onboarding, reaches dashboard."
  tags: [onboarding, critical_path, smoke]

  # Which personas run this journey
  personas:
    - ref: alex_developer              # References personas/{name}.yaml
      role: primary                    # primary | observer (future)

  # Services that must be healthy before steps run
  preconditions:
    - service: frontend
      check: /
      expected_status: 200
    - service: backend
      check: /api/health
      expected_status: 200

  # Holdout flag -- if true, scenario is skipped during normal runs
  holdout: false

  # Steps executed in order
  steps:
    - id: visit_homepage
      mode: browser                    # browser | api | native_app | ios_simulator
      goal: "Navigate to the homepage and verify it loads"
      max_actions: 20                  # Max AI actions before step fails (default: 30)
      max_duration_seconds: 120        # Timeout per step (default: 180)
      checkpoints:
        - type: text_present
          value: "Welcome"

    - id: fill_signup_form
      mode: browser
      goal: "Fill out the signup form with email {{persona.credentials.email}} and password {{persona.credentials.password}}"
      checkpoints:
        - type: text_present
          value: "email"
```

### Step Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | required | Unique step identifier |
| `mode` | string | `browser` | Execution mode: `browser`, `api`, `native_app`, `ios_simulator` |
| `goal` | string | required | Natural language goal for the AI |
| `max_actions` | int | 30 | Maximum AI actions before the step is failed |
| `max_duration_seconds` | int | 180 | Timeout in seconds |
| `url` | string | -- | Initial URL to navigate to (browser mode) |
| `viewport` | string | -- | Override viewport for this step (e.g., `mobile`) |
| `checkpoints` | list | -- | Conditions to verify during the step |

### Checkpoint Types

| Type | Description |
|------|-------------|
| `text_present` | Verify that specific text appears on the page |
| `url_contains` | Verify the current URL contains a string |
| `element_visible` | Verify an element matching a description is visible |

### Template Variables

Journey steps support `{{dotpath}}` template variables that resolve at runtime:

- `{{persona.credentials.email}}` -- Persona's email
- `{{persona.credentials.password}}` -- Persona's password
- `{{persona.name}}` -- Persona name
- `{{run_id}}` -- Current run ID (useful for unique test data)

Variables from earlier API steps are also available in later steps (captured variables).

### Tags and Levels

Tags let you organize scenarios. The `--level` flag filters how many run:

- `smoke` -- Runs only the first scenario (fast CI check)
- `standard` -- Runs all matching scenarios (default)
- `thorough` -- Runs all scenarios (same as standard currently; reserved for future multi-viewport/multi-persona expansion)

## Stuck Detection Tuning

Each step can override stuck detection thresholds:

```yaml
steps:
  - id: complex_interaction
    mode: browser
    goal: "Complete the multi-step wizard"
    stuck_warn_threshold: 8        # Default: 5 -- consecutive identical UI states before warning
    stuck_abort_threshold: 15      # Default: 10 -- consecutive identical UI states before abort
    action_repeat_threshold: 5     # Default: 3 -- same action repeated before warning
```

Increase these for apps with slow animations or delayed state changes. Decrease them for fast, responsive UIs where stuck detection should be aggressive.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (highest priority) |
| `SPECTERQA_PROJECT_DIR` | Override project directory location |
| `SPECTERQA_HEADLESS` | Override headless mode (`true`/`false`) |
| `SPECTERQA_BUDGET` | Override default budget |
