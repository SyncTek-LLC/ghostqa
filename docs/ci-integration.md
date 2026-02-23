# CI Integration

SpecterQA is designed to run in CI. It operates headless by default, produces JUnit XML reports, and returns meaningful exit codes.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All tests passed |
| `1` | One or more tests failed (UX issues, failed steps) |
| `2` | Configuration error (missing product config, bad YAML, missing API key) |
| `3` | Infrastructure error (Playwright not installed, API unreachable, import failure) |

## JUnit XML Output

Add `--junit-xml` to any run command to produce a JUnit-compatible XML report:

```bash
specterqa run -p myapp --junit-xml results.xml
```

The XML follows the standard JUnit format. Each step becomes a `<testcase>`, failures include the error description. This plugs into any CI system that reads JUnit XML (which is basically all of them).

## JSON Output

For programmatic consumption, use `--output json`:

```bash
specterqa run -p myapp --output json > results.json
```

JSON goes to stdout; human-readable progress goes to stderr. This means you can pipe JSON to a file while still seeing progress in CI logs.

The JSON structure:

```json
{
  "run_id": "GQA-RUN-20260222-143052-a1b2",
  "passed": true,
  "scenario_name": "Onboarding Happy Path",
  "product_name": "myapp",
  "duration_seconds": 45.2,
  "cost_usd": 0.4521,
  "step_reports": [
    {
      "step_id": "visit_homepage",
      "description": "Navigate to the homepage",
      "passed": true,
      "duration_seconds": 12.3,
      "error": null
    }
  ],
  "findings": [
    {
      "severity": "medium",
      "category": "ux",
      "description": "No loading indicator when page transitions",
      "step_id": "navigate_signup"
    }
  ],
  "cost_summary": {
    "total_cost_usd": 0.4521,
    "calls_by_model": {
      "claude-haiku-4-5-20251001": 15,
      "claude-sonnet-4-20250514": 8
    }
  }
}
```

## GitHub Actions

```yaml
name: SpecterQA Behavioral Tests

on:
  pull_request:
  push:
    branches: [main]

jobs:
  specterqa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install SpecterQA
        run: |
          pip install specterqa
          specterqa install

      - name: Run behavioral tests
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          specterqa run -p myapp \
            --level smoke \
            --budget 2.00 \
            --junit-xml results.xml

      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: specterqa-results
          path: |
            results.xml
            .specterqa/evidence/

      - name: Publish JUnit results
        uses: dorny/test-reporter@v1
        if: always()
        with:
          name: SpecterQA Results
          path: results.xml
          reporter: java-junit
```

### Tips for GitHub Actions

- **Store your API key as a secret.** Never hardcode it in the workflow file.
- **Use `--level smoke` for PR checks.** Smoke tests run one scenario and cost ~$0.30-0.60. Full runs on merge to main.
- **Set a tight `--budget`.** $2.00 is usually plenty for a smoke test. This prevents runaway costs if something goes wrong.
- **Upload the evidence directory.** The screenshots and findings are invaluable for debugging failures.
- **Start your app first.** SpecterQA needs your app running. Add a step to start it and wait for the health endpoint before running tests.

### Starting Your App in CI

A common pattern:

```yaml
      - name: Start app
        run: |
          npm start &
          # Wait for app to be ready
          for i in $(seq 1 30); do
            curl -s http://localhost:3000 > /dev/null && break
            sleep 1
          done
```

## GitLab CI

```yaml
specterqa:
  image: python:3.12
  stage: test
  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY
  before_script:
    - pip install specterqa
    - specterqa install
  script:
    - specterqa run -p myapp --level smoke --budget 2.00 --junit-xml results.xml
  artifacts:
    when: always
    reports:
      junit: results.xml
    paths:
      - .specterqa/evidence/
    expire_in: 7 days
```

## CircleCI

```yaml
version: 2.1

jobs:
  specterqa:
    docker:
      - image: cimg/python:3.12
    steps:
      - checkout
      - run:
          name: Install SpecterQA
          command: |
            pip install specterqa
            specterqa install
      - run:
          name: Run behavioral tests
          command: |
            specterqa run -p myapp \
              --level smoke \
              --budget 2.00 \
              --junit-xml results.xml
      - store_test_results:
          path: results.xml
      - store_artifacts:
          path: .specterqa/evidence/
```

## Budget Management in CI

CI environments are where budget enforcement matters most. A misconfigured journey or a stuck AI can burn through API credits fast.

**Recommendations:**

| Environment | Level | Budget | When |
|-------------|-------|--------|------|
| PR checks | `smoke` | $2.00 | Every push |
| Merge to main | `standard` | $5.00 | On merge |
| Nightly | `standard` | $10.00 | Scheduled |

Set per-day and per-month caps in your product config:

```yaml
cost_limits:
  per_run_usd: 5.00
  per_day_usd: 20.00
  per_month_usd: 200.00
```

The cost ledger (`.specterqa/costs.jsonl`) tracks cumulative spend. If the daily or monthly cap is hit, subsequent runs will refuse to start.

## Headless Mode

SpecterQA runs headless by default (`--headless` is the default). In CI, this is what you want. If you need to debug locally with a visible browser:

```bash
specterqa run -p myapp --no-headless
```

## Playwright in CI

`specterqa install` runs `playwright install --with-deps chromium` under the hood. On Ubuntu-based CI images, this installs the necessary system dependencies automatically. If you're using a minimal Docker image, you may need to install additional packages:

```dockerfile
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2
```

Or just use the official Playwright Docker image as your base.

## Viewing Evidence

After a CI run, the evidence directory contains:

```
.specterqa/evidence/GQA-RUN-20260222-143052-a1b2/
  run-result.json      # Structured results (JSON)
  run-meta.json        # Run metadata
  run-status.json      # Final status
  report.md            # Human-readable report
  *.png                # Screenshots from each action
```

Upload these as artifacts so you can review failures without re-running.
