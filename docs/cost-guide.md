# Cost Guide

SpecterQA uses the Anthropic Claude API for its vision-based testing. Every run costs money. This guide helps you understand what you'll spend and how to control it.

## How Costs Work

Each time SpecterQA takes an action, it:

1. Takes a screenshot (~200-500KB PNG)
2. Sends the screenshot + context to a Claude vision model
3. Receives a decision (small text response)

You pay for the tokens in and out of each API call. Screenshots are the expensive part -- they consume a lot of input tokens.

## Model Pricing

SpecterQA uses tiered model routing to keep costs down:

| Model | Input (per 1M tokens) | Output (per 1M tokens) | Used For |
|-------|----------------------|------------------------|----------|
| Claude Haiku 4.5 | $0.80 | $4.00 | Simple navigation (click, scroll, wait) |
| Claude Sonnet 4 | $3.00 | $15.00 | Complex reasoning (form fills, initial assessment, stuck recovery) |
| Claude Opus 4 | $15.00 | $75.00 | Not used by default -- available for `persona_heavy` routing |
| Ollama llava:13b | Free | Free | Local model fallback for zero-cost simple actions |

**Prices are as of February 2026.** Check [Anthropic's pricing page](https://www.anthropic.com/pricing) for current rates.

## Typical Costs

These are real-world numbers from production runs:

### Per-Action Costs

| Action Type | Model Used | Typical Cost |
|-------------|-----------|--------------|
| Simple click/scroll | Haiku | ~$0.005-0.01 |
| Form fill | Sonnet | ~$0.02-0.05 |
| Initial page assessment | Sonnet | ~$0.03-0.06 |
| Periodic checkpoint | Sonnet | ~$0.02-0.04 |
| Local navigation | Ollama | $0.00 |

### Per-Run Costs

| Scenario Type | Steps | Actions | Typical Cost |
|---------------|-------|---------|--------------|
| 3-step smoke test | 3 | ~15-25 | $0.30-0.60 |
| 5-step standard journey | 5 | ~25-40 | $0.50-1.50 |
| 10-step complex journey with forms | 10 | ~50-80 | $1.00-3.00 |
| Adversarial security probing | 5 | ~30-50 | $0.80-2.00 |

### What Drives Costs Up

- **More steps** -- Each step resets the AI conversation, so the initial assessment (Sonnet-priced) happens again.
- **Complex forms** -- Fill actions always use Sonnet. A form with 10 fields costs more than a form with 3.
- **Getting stuck** -- When the AI gets stuck, the engine escalates to the stronger model and retries. A 3-action stuck loop can cost 5-10x a normal action.
- **Large pages** -- Screenshots of content-heavy pages produce more tokens. A dashboard with 50 elements costs more to interpret than a simple login page.
- **Conversation history** -- The AI maintains conversation history within a step. Later actions in a long step include more history tokens. SpecterQA mitigates this by compacting old screenshots (replacing base64 data with text summaries), but costs still grow as actions accumulate.

### What Keeps Costs Down

- **Model routing** -- Simple clicks use Haiku ($0.80/M input) instead of Sonnet ($3/M input). This is automatic.
- **Local Ollama** -- If you run a local llava:13b model, simple navigation actions route there for zero API cost.
- **Screenshot compaction** -- After 3 screenshots in a step's history, older ones are replaced with text summaries. This prevents unbounded history growth.
- **Smoke level** -- `--level smoke` runs only the first scenario. Good for quick CI checks.
- **Tight budgets** -- `--budget 2.00` hard-stops the run at $2. Better to fail fast than overspend.

## Budget Enforcement

SpecterQA has three layers of budget enforcement:

### Per-Run Budget

Set via CLI or config. The engine raises `BudgetExceededError` and stops immediately if exceeded.

```bash
specterqa run -p myapp --budget 5.00
```

Or in `products/myapp.yaml`:

```yaml
cost_limits:
  per_run_usd: 5.00
  warn_at_pct: 80    # Logs a warning at 80% of budget
```

When the warning threshold is hit, you'll see it in the logs. When the hard cap is hit, the current step is aborted and the run ends with a "budget exceeded" finding.

### Per-Day Budget

Tracked in `.specterqa/costs.jsonl`. Before each run, SpecterQA sums today's costs and refuses to start if the daily cap is exceeded.

```yaml
cost_limits:
  per_day_usd: 20.00
```

### Per-Month Budget

Same mechanism, summing the current calendar month:

```yaml
cost_limits:
  per_month_usd: 200.00
```

### The Cost Ledger

Every completed run appends an entry to `.specterqa/costs.jsonl`:

```json
{"timestamp":"2026-02-22T14:31:37+00:00","run_id":"GQA-RUN-20260222-143052-a1b2","product":"myapp","level":"smoke","cost_usd":0.4521}
```

This file is the source of truth for cumulative budget checks. Don't delete it unless you want to reset your budget tracking.

## Cost Optimization Strategies

### For CI

1. **Use `--level smoke` for PR checks.** One scenario, ~$0.30-0.60 per run.
2. **Reserve `standard` for merge-to-main.** Run the full suite less often.
3. **Set per-day caps.** If a CI loop goes haywire, the daily cap stops the bleeding.
4. **Use tight per-run budgets.** $2 is plenty for a smoke test. $5 for standard. $10 for thorough.

### For Local Development

1. **Run Ollama locally.** Install [Ollama](https://ollama.ai) and pull `llava:13b`. SpecterQA routes simple navigation there automatically.
2. **Use `--level smoke` while iterating.** Run the full suite only when you think you're done.
3. **Watch the budget summary.** After each run, SpecterQA prints the total cost. If a journey consistently costs more than expected, check for stuck loops.

### For Adversarial/Security Testing

Adversarial personas explore more, probe edge cases, and get stuck more often. Expect 2-3x the cost of a standard journey. Set budgets accordingly:

```yaml
cost_limits:
  per_run_usd: 10.00
```

## Monitoring Spend

### After Each Run

Every run prints a summary:

```
  Steps:     3/3 passed
  Findings:  1
  Duration:  45.2s
  Cost:      $0.4521
  Run ID:    GQA-RUN-20260222-143052-a1b2
```

### JSON Output Includes Cost Breakdown

```json
{
  "cost_summary": {
    "total_cost_usd": 0.4521,
    "calls_by_model": {
      "claude-haiku-4-5-20251001": 15,
      "claude-sonnet-4-20250514": 8
    },
    "cost_by_model": {
      "claude-haiku-4-5-20251001": 0.134,
      "claude-sonnet-4-20250514": 0.318
    },
    "budget_limit_usd": 5.0,
    "budget_remaining_usd": 4.5479
  }
}
```

### Programmatic Budget Check

```python
from pathlib import Path
from specterqa.engine.cost_tracker import CostTracker

status = CostTracker.check_cumulative_budget(
    base_dir=Path(".specterqa"),
    per_day_usd=20.00,
    per_month_usd=200.00,
)

print(f"Today: ${status['daily_spent']:.2f} / ${status['daily_limit']:.2f}")
print(f"Month: ${status['monthly_spent']:.2f} / ${status['monthly_limit']:.2f}")
```

## Estimating Costs Before a Run

A rough formula:

```
cost ≈ (num_steps * 5 actions * $0.01) + (num_form_fills * $0.05) + (num_steps * $0.04 initial assessment)
```

For a 5-step journey with 2 form fills:

```
(5 * 5 * $0.01) + (2 * $0.05) + (5 * $0.04) = $0.25 + $0.10 + $0.20 = $0.55
```

This is approximate. Actual costs depend on page complexity, how many actions the AI needs, and whether it gets stuck anywhere.

## FAQ

**Q: Can I use SpecterQA without paying for API calls?**

If you run a local Ollama model (llava:13b), SpecterQA can route simple actions there for free. But the initial assessment and form fills still need a capable vision model. There's no fully-free mode today.

**Q: What happens if my API key has no credits?**

The Anthropic SDK will return an error. SpecterQA catches it and reports it as an infrastructure failure (exit code 3). No partial results.

**Q: Can I use a different API provider?**

Not out of the box. SpecterQA uses the Anthropic Python SDK directly. You can implement the `AIDecider` protocol with any model provider and use the `AIStepRunner` directly -- see [for-agents.md](for-agents.md).

**Q: Why is my run more expensive than expected?**

Most likely the AI got stuck somewhere. Check the evidence directory for screenshots -- you'll see repeated similar screenshots where the AI was trying different approaches. Increase `stuck_abort_threshold` or simplify the goal to reduce this.
