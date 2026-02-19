"""Unit tests for ghostqa.models — Model constants and pricing."""

from __future__ import annotations

from ghostqa.models import (
    DEFAULT_BUDGET_USD,
    DEFAULT_STEP_TIMEOUT,
    DEFAULT_VIEWPORT,
    MAX_ACTIONS_PER_STEP,
    MODELS,
    PRICING,
)


# ---------------------------------------------------------------------------
# 1. MODELS dict completeness
# ---------------------------------------------------------------------------

class TestModelsDict:
    """MODELS dict should contain all expected tier keys."""

    def test_persona_simple_exists(self):
        assert "persona_simple" in MODELS

    def test_persona_complex_exists(self):
        assert "persona_complex" in MODELS

    def test_persona_heavy_exists(self):
        assert "persona_heavy" in MODELS

    def test_analysis_exists(self):
        assert "analysis" in MODELS

    def test_all_model_values_are_strings(self):
        for key, value in MODELS.items():
            assert isinstance(value, str), f"MODELS['{key}'] is not a string"

    def test_model_ids_contain_claude(self):
        """All model IDs should reference Claude models."""
        for key, model_id in MODELS.items():
            assert "claude" in model_id, f"MODELS['{key}'] = '{model_id}' does not contain 'claude'"


# ---------------------------------------------------------------------------
# 2. PRICING dict matches MODELS
# ---------------------------------------------------------------------------

class TestPricingDict:
    """Every model in MODELS should have a corresponding pricing entry."""

    def test_all_models_have_pricing(self):
        for tier, model_id in MODELS.items():
            assert model_id in PRICING, (
                f"Model '{model_id}' (tier: {tier}) has no entry in PRICING"
            )

    def test_pricing_has_input_and_output(self):
        for model_id, prices in PRICING.items():
            assert "input" in prices, f"PRICING['{model_id}'] missing 'input'"
            assert "output" in prices, f"PRICING['{model_id}'] missing 'output'"

    def test_pricing_values_are_positive(self):
        for model_id, prices in PRICING.items():
            assert prices["input"] > 0, f"PRICING['{model_id}']['input'] is not positive"
            assert prices["output"] > 0, f"PRICING['{model_id}']['output'] is not positive"

    def test_output_pricing_exceeds_input(self):
        """Output tokens are typically more expensive than input tokens."""
        for model_id, prices in PRICING.items():
            assert prices["output"] >= prices["input"], (
                f"PRICING['{model_id}'] has output < input, which is unusual"
            )


# ---------------------------------------------------------------------------
# 3. DEFAULT_BUDGET_USD
# ---------------------------------------------------------------------------

class TestDefaultBudget:
    """DEFAULT_BUDGET_USD should be a positive float."""

    def test_budget_is_positive(self):
        assert DEFAULT_BUDGET_USD > 0

    def test_budget_is_numeric(self):
        assert isinstance(DEFAULT_BUDGET_USD, (int, float))

    def test_budget_is_reasonable(self):
        """Budget should be between $0.01 and $100 for sanity."""
        assert 0.01 <= DEFAULT_BUDGET_USD <= 100


# ---------------------------------------------------------------------------
# 4. DEFAULT_VIEWPORT
# ---------------------------------------------------------------------------

class TestDefaultViewport:
    """DEFAULT_VIEWPORT should be a valid (width, height) tuple."""

    def test_viewport_is_tuple(self):
        assert isinstance(DEFAULT_VIEWPORT, tuple)

    def test_viewport_has_two_elements(self):
        assert len(DEFAULT_VIEWPORT) == 2

    def test_viewport_width_is_positive_int(self):
        assert isinstance(DEFAULT_VIEWPORT[0], int)
        assert DEFAULT_VIEWPORT[0] > 0

    def test_viewport_height_is_positive_int(self):
        assert isinstance(DEFAULT_VIEWPORT[1], int)
        assert DEFAULT_VIEWPORT[1] > 0

    def test_viewport_dimensions_are_standard(self):
        """Should be at least 320x240 (minimum usable) and at most 7680x4320 (8K)."""
        w, h = DEFAULT_VIEWPORT
        assert 320 <= w <= 7680
        assert 240 <= h <= 4320


# ---------------------------------------------------------------------------
# 5. Timeout and max-action constants
# ---------------------------------------------------------------------------

class TestTimeoutConstants:
    """Timeout and max-action constants should be reasonable."""

    def test_step_timeout_is_positive(self):
        assert DEFAULT_STEP_TIMEOUT > 0

    def test_run_timeout_at_least_step_timeout(self):
        from ghostqa.models import DEFAULT_RUN_TIMEOUT
        assert DEFAULT_RUN_TIMEOUT >= DEFAULT_STEP_TIMEOUT

    def test_max_actions_is_positive(self):
        assert MAX_ACTIONS_PER_STEP > 0
