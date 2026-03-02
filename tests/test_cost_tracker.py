"""Unit tests for specterqa.engine.cost_tracker — CostTracker and cost accounting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specterqa.engine.cost_tracker import (
    APICall,
    BudgetExceededError,
    CostSummary,
    CostTracker,
    MODEL_PRICING,
    _FALLBACK_MODEL,
)
from specterqa.models import PRICING


# ---------------------------------------------------------------------------
# 1. CostTracker initialization
# ---------------------------------------------------------------------------

class TestCostTrackerInit:
    """CostTracker should initialize with correct defaults and provided values."""

    def test_default_initialization(self):
        ct = CostTracker()
        assert ct.total_cost == 0.0
        assert ct.calls == []
        assert ct.warning_issued is False
        assert ct.budget_exceeded is False

    def test_custom_budget(self):
        ct = CostTracker(per_run_usd=25.0)
        summary = ct.get_summary()
        assert summary.budget_limit_usd == 25.0

    def test_custom_warn_threshold(self):
        ct = CostTracker(per_run_usd=1.0, warn_at_pct=50)
        # Record enough to trigger 50% warning
        ct.record_call("claude-haiku-4-5-20251001", 1_000_000, 0, purpose="test")
        # Haiku input: $0.80 per 1M tokens -> $0.80 cost, that's 80% of $1.00
        assert ct.warning_issued is True


# ---------------------------------------------------------------------------
# 2. record_call() — basic tracking
# ---------------------------------------------------------------------------

class TestRecordCall:
    """record_call() should correctly track costs and return APICall records."""

    def test_record_call_returns_api_call(self):
        ct = CostTracker(per_run_usd=100.0)
        call = ct.record_call("claude-haiku-4-5-20251001", 1000, 500, purpose="test")
        assert isinstance(call, APICall)
        assert call.model == "claude-haiku-4-5-20251001"
        assert call.input_tokens == 1000
        assert call.output_tokens == 500
        assert call.purpose == "test"

    def test_record_call_accumulates_tokens(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 1000, 500)
        ct.record_call("claude-haiku-4-5-20251001", 2000, 1000)
        summary = ct.get_summary()
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1500

    def test_record_call_accumulates_cost(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 1000, 500)
        assert ct.total_cost > 0

    def test_calls_list_grows(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 100, 50)
        ct.record_call("claude-haiku-4-5-20251001", 200, 100)
        assert len(ct.calls) == 2


# ---------------------------------------------------------------------------
# 3. Budget warning threshold
# ---------------------------------------------------------------------------

class TestBudgetWarning:
    """Warning should be issued when cost reaches the threshold percentage."""

    def test_no_warning_below_threshold(self):
        ct = CostTracker(per_run_usd=100.0, warn_at_pct=80)
        # Small call, well under 80%
        ct.record_call("claude-haiku-4-5-20251001", 100, 50)
        assert ct.warning_issued is False

    def test_warning_at_threshold(self):
        # Sonnet: $3.00 input per 1M, $15.00 output per 1M
        # 1M input tokens = $3.00 cost. Budget $3.50, warn at 80% = $2.80
        ct = CostTracker(per_run_usd=3.50, warn_at_pct=80)
        ct.record_call("claude-sonnet-4-20250514", 1_000_000, 0, purpose="big_call")
        # Cost: $3.00, which is 85.7% of $3.50
        assert ct.warning_issued is True

    def test_warning_not_issued_when_budget_is_zero(self):
        ct = CostTracker(per_run_usd=0.0, warn_at_pct=80)
        ct.record_call("claude-haiku-4-5-20251001", 100_000, 50_000)
        assert ct.warning_issued is False


# ---------------------------------------------------------------------------
# 4. Budget enforcement — hard cap
# ---------------------------------------------------------------------------

class TestBudgetEnforcement:
    """Exceeding per_run_usd should raise BudgetExceededError."""

    def test_raises_when_budget_exceeded(self):
        # Budget: $0.01, a small call will exceed it
        ct = CostTracker(per_run_usd=0.001)
        with pytest.raises(BudgetExceededError, match="Run budget exceeded"):
            # Haiku: $0.80/1M input -> 100K tokens = $0.08 > $0.001
            ct.record_call("claude-haiku-4-5-20251001", 100_000, 0)

    def test_budget_exceeded_flag_set(self):
        ct = CostTracker(per_run_usd=0.001)
        try:
            ct.record_call("claude-haiku-4-5-20251001", 100_000, 0)
        except BudgetExceededError:
            pass
        assert ct.budget_exceeded is True

    def test_no_exception_within_budget(self):
        ct = CostTracker(per_run_usd=100.0)
        # Should not raise
        ct.record_call("claude-haiku-4-5-20251001", 1000, 500)
        assert ct.budget_exceeded is False


# ---------------------------------------------------------------------------
# 5. Cost calculation accuracy
# ---------------------------------------------------------------------------

class TestCostCalculation:
    """_calculate_cost() should produce accurate USD costs for known token counts."""

    def test_haiku_cost_calculation(self):
        # Haiku: $0.80 input, $4.00 output per 1M tokens
        cost = CostTracker._calculate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        expected = 0.80 + 4.00  # $4.80
        assert abs(cost - expected) < 0.0001

    def test_sonnet_cost_calculation(self):
        # Sonnet: $3.00 input, $15.00 output per 1M tokens
        cost = CostTracker._calculate_cost("claude-sonnet-4-20250514", 500_000, 200_000)
        expected = (500_000 / 1_000_000) * 3.00 + (200_000 / 1_000_000) * 15.00
        assert abs(cost - expected) < 0.0001

    def test_opus_cost_calculation(self):
        # Opus: $15.00 input, $75.00 output per 1M tokens
        cost = CostTracker._calculate_cost("claude-opus-4-6", 100_000, 50_000)
        expected = (100_000 / 1_000_000) * 15.00 + (50_000 / 1_000_000) * 75.00
        assert abs(cost - expected) < 0.0001

    def test_zero_tokens_zero_cost(self):
        cost = CostTracker._calculate_cost("claude-haiku-4-5-20251001", 0, 0)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# 6. get_summary() — aggregate correctness
# ---------------------------------------------------------------------------

class TestGetSummary:
    """get_summary() should return a CostSummary with correct aggregate values."""

    def test_summary_after_multiple_calls(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 1000, 500, purpose="a")
        ct.record_call("claude-sonnet-4-20250514", 2000, 1000, purpose="b")

        summary = ct.get_summary()
        assert isinstance(summary, CostSummary)
        assert summary.call_count == 2
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1500
        assert summary.total_cost_usd > 0
        assert summary.budget_limit_usd == 100.0
        assert summary.budget_remaining_usd > 0
        assert summary.budget_exceeded is False

    def test_summary_calls_by_model(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 1000, 500)
        ct.record_call("claude-haiku-4-5-20251001", 2000, 1000)
        ct.record_call("claude-sonnet-4-20250514", 3000, 1500)

        summary = ct.get_summary()
        assert summary.calls_by_model["claude-haiku-4-5-20251001"] == 2
        assert summary.calls_by_model["claude-sonnet-4-20250514"] == 1

    def test_summary_cost_by_model(self):
        ct = CostTracker(per_run_usd=100.0)
        ct.record_call("claude-haiku-4-5-20251001", 1_000_000, 0)
        ct.record_call("claude-sonnet-4-20250514", 1_000_000, 0)

        summary = ct.get_summary()
        assert abs(summary.cost_by_model["claude-haiku-4-5-20251001"] - 0.80) < 0.0001
        assert abs(summary.cost_by_model["claude-sonnet-4-20250514"] - 3.00) < 0.0001

    def test_summary_empty_tracker(self):
        ct = CostTracker(per_run_usd=10.0)
        summary = ct.get_summary()
        assert summary.call_count == 0
        assert summary.total_cost_usd == 0.0
        assert summary.budget_remaining_usd == 10.0


# ---------------------------------------------------------------------------
# 7. Ollama model — zero cost
# ---------------------------------------------------------------------------

class TestOllamaZeroCost:
    """Ollama models should have zero pricing (local hardware)."""

    def test_ollama_model_in_pricing(self):
        assert "ollama:llava:13b" in MODEL_PRICING

    def test_ollama_model_zero_cost(self):
        input_price, output_price = MODEL_PRICING["ollama:llava:13b"]
        assert input_price == 0.0
        assert output_price == 0.0

    def test_ollama_call_has_zero_cost(self):
        ct = CostTracker(per_run_usd=1.0)
        call = ct.record_call("ollama:llava:13b", 10_000, 5_000, purpose="local")
        assert call.cost_usd == 0.0
        assert ct.total_cost == 0.0


# ---------------------------------------------------------------------------
# 8. Fallback model pricing for unknown models
# ---------------------------------------------------------------------------

class TestFallbackPricing:
    """Unknown models should fall back to Sonnet pricing."""

    def test_unknown_model_uses_sonnet_pricing(self):
        cost = CostTracker._calculate_cost("unknown-model-v1", 1_000_000, 0)
        expected = MODEL_PRICING[_FALLBACK_MODEL][0]  # Sonnet input pricing
        assert abs(cost - expected) < 0.0001

    def test_fallback_model_is_sonnet(self):
        assert "sonnet" in _FALLBACK_MODEL


# ---------------------------------------------------------------------------
# 9. Persistent ledger — record_run_cost()
# ---------------------------------------------------------------------------

class TestRecordRunCost:
    """record_run_cost() should append valid JSONL entries to the cost ledger."""

    def test_creates_ledger_file(self, tmp_path: Path):
        ledger = CostTracker.record_run_cost(
            base_dir=tmp_path,
            run_id="RUN-001",
            product="testapp",
            cost_usd=1.234,
            level="smoke",
        )
        assert ledger.exists()
        assert ledger.name == "costs.jsonl"

    def test_entry_is_valid_json(self, tmp_path: Path):
        CostTracker.record_run_cost(
            base_dir=tmp_path, run_id="RUN-001", product="testapp", cost_usd=1.0
        )
        ledger = tmp_path / "costs.jsonl"
        line = ledger.read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["run_id"] == "RUN-001"
        assert entry["product"] == "testapp"
        assert entry["cost_usd"] == 1.0

    def test_appends_multiple_entries(self, tmp_path: Path):
        CostTracker.record_run_cost(tmp_path, "RUN-001", "app1", 1.0)
        CostTracker.record_run_cost(tmp_path, "RUN-002", "app1", 2.0)
        ledger = tmp_path / "costs.jsonl"
        lines = [l for l in ledger.read_text(encoding="utf-8").strip().split("\n") if l]
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# 10. Cumulative budget checking
# ---------------------------------------------------------------------------

class TestCheckCumulativeBudget:
    """check_cumulative_budget() should aggregate daily/monthly spend from ledger."""

    def test_no_ledger_returns_zero_spend(self, tmp_path: Path):
        result = CostTracker.check_cumulative_budget(tmp_path, per_day_usd=10.0)
        assert result["daily_spent"] == 0.0
        assert result["monthly_spent"] == 0.0
        assert result["daily_ok"] is True

    def test_zero_limits_always_ok(self, tmp_path: Path):
        result = CostTracker.check_cumulative_budget(tmp_path, per_day_usd=0.0, per_month_usd=0.0)
        assert result["daily_ok"] is True
        assert result["monthly_ok"] is True
