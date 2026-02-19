"""Centralized model configuration and pricing."""

# Model IDs for different tiers
MODELS = {
    "persona_simple": "claude-haiku-4-5-20251001",
    "persona_complex": "claude-sonnet-4-20250514",
    "persona_heavy": "claude-opus-4-20250115",
    "analysis": "claude-haiku-4-5-20251001",
}

# Pricing per million tokens (USD)
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-opus-4-20250115": {"input": 15.00, "output": 75.00},
}

# Default budget per run
DEFAULT_BUDGET_USD = 5.00

# Default viewport
DEFAULT_VIEWPORT = (1280, 720)

# Timeouts
DEFAULT_STEP_TIMEOUT = 300  # seconds
DEFAULT_RUN_TIMEOUT = 600  # seconds
MAX_ACTIONS_PER_STEP = 40
