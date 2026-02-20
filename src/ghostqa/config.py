"""GhostQA configuration management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ghostqa.models import DEFAULT_BUDGET_USD, DEFAULT_VIEWPORT, MODELS


class GhostQAConfigError(Exception):
    """Raised when configuration is invalid or missing."""

    pass


@dataclass
class GhostQAConfig:
    """Configuration for a GhostQA run."""

    # Required
    product_name: str = ""
    base_url: str = ""

    # Paths
    project_dir: Path = field(default_factory=lambda: Path(".ghostqa"))
    products_dir: Path = field(default_factory=lambda: Path(".ghostqa/products"))
    personas_dir: Path = field(default_factory=lambda: Path(".ghostqa/personas"))
    journeys_dir: Path = field(default_factory=lambda: Path(".ghostqa/journeys"))
    evidence_dir: Path = field(default_factory=lambda: Path(".ghostqa/evidence"))
    fixtures_dir: Path | None = None

    # API
    anthropic_api_key: str = ""
    model_persona_simple: str = MODELS["persona_simple"]
    model_persona_complex: str = MODELS["persona_complex"]
    model_analysis: str = MODELS["analysis"]

    # Behavior
    budget: float = DEFAULT_BUDGET_USD
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    headless: bool = True
    timeout: int = 600
    level: str = "standard"

    # Optional integrations (for BusinessAtlas adapter)
    cost_ledger_path: Path | None = None
    system_ledger_path: Path | None = None
    initiative_id: str | None = None

    @classmethod
    def from_file(cls, config_path: Path) -> GhostQAConfig:
        """Load config from a YAML file."""
        if not config_path.exists():
            raise GhostQAConfigError(f"Config file not found: {config_path}\n\nTo fix: ghostqa init")
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return cls._from_dict(data, config_path.parent)

    @classmethod
    def _from_dict(cls, data: dict[str, Any], project_dir: Path) -> GhostQAConfig:
        """Create config from a dictionary."""
        config = cls()
        config.project_dir = project_dir

        # Map YAML keys to config fields
        if "products_dir" in data:
            config.products_dir = project_dir / data["products_dir"]
        else:
            config.products_dir = project_dir / "products"

        if "personas_dir" in data:
            config.personas_dir = project_dir / data["personas_dir"]
        else:
            config.personas_dir = project_dir / "personas"

        if "journeys_dir" in data:
            config.journeys_dir = project_dir / data["journeys_dir"]
        else:
            config.journeys_dir = project_dir / "journeys"

        if "evidence_dir" in data:
            config.evidence_dir = project_dir / data["evidence_dir"]
        else:
            config.evidence_dir = project_dir / "evidence"

        if "budget" in data:
            config.budget = float(data["budget"])
        if "headless" in data:
            config.headless = bool(data["headless"])
        if "timeout" in data:
            config.timeout = int(data["timeout"])
        if "viewport" in data:
            vp = data["viewport"]
            if isinstance(vp, dict):
                config.viewport = (vp.get("width", 1280), vp.get("height", 720))

        return config

    def resolve_product(self, product_name: str) -> dict:
        """Load a product config by name."""
        product_path = self.products_dir / f"{product_name}.yaml"
        if not product_path.exists():
            raise GhostQAConfigError(
                f"Product not found: {product_name}\n\n"
                f"Expected file: {product_path}\n"
                "To fix: Create the product config file"
            )
        with open(product_path) as f:
            return yaml.safe_load(f)
