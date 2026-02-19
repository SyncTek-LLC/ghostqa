"""Shared fixtures for GhostQA unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixture: temporary project directory with .ghostqa/ structure
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a temporary .ghostqa/ project directory with full structure."""
    ghostqa_dir = tmp_path / ".ghostqa"
    for sub in ("products", "personas", "journeys", "evidence"):
        (ghostqa_dir / sub).mkdir(parents=True)

    # Write a minimal valid config
    config_data = {
        "budget": 5.00,
        "headless": True,
        "viewport": {"width": 1280, "height": 720},
        "timeout": 600,
    }
    (ghostqa_dir / "config.yaml").write_text(
        yaml.dump(config_data, default_flow_style=False), encoding="utf-8"
    )

    return ghostqa_dir


# ---------------------------------------------------------------------------
# Fixture: sample config YAML string
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_config_yaml() -> str:
    """Return a valid GhostQA config.yaml as a string."""
    return """\
budget: 5.00
headless: true
viewport:
  width: 1920
  height: 1080
timeout: 300
products_dir: products
personas_dir: personas
journeys_dir: journeys
evidence_dir: evidence
"""


# ---------------------------------------------------------------------------
# Fixture: sample product YAML string
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_product_yaml() -> str:
    """Return a valid product YAML config as a string."""
    return """\
product:
  name: testapp
  display_name: "Test Application"
  base_url: "http://localhost:3000"
  services:
    frontend:
      url: "http://localhost:3000"
      health_endpoint: /
  viewports:
    desktop:
      width: 1280
      height: 720
  cost_limits:
    per_run_usd: 5.00
"""
