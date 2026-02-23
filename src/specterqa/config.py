"""SpecterQA configuration management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from specterqa.models import DEFAULT_BUDGET_USD, DEFAULT_VIEWPORT, MODELS


class SpecterQAConfigError(Exception):
    """Raised when configuration is invalid or missing."""

    pass


@dataclass
class SpecterQAConfig:
    """Configuration for a SpecterQA run."""

    # Required
    product_name: str = ""
    base_url: str = ""

    # Paths
    project_dir: Path = field(default_factory=lambda: Path(".specterqa"))
    products_dir: Path = field(default_factory=lambda: Path(".specterqa/products"))
    personas_dir: Path = field(default_factory=lambda: Path(".specterqa/personas"))
    journeys_dir: Path = field(default_factory=lambda: Path(".specterqa/journeys"))
    evidence_dir: Path = field(default_factory=lambda: Path(".specterqa/evidence"))
    fixtures_dir: Path | None = None

    # API
    # SECURITY (FIND-004): repr=False prevents the API key from appearing in
    # repr() output, debug logs, and error tracebacks that print the config object.
    anthropic_api_key: str = field(default="", repr=False)
    model_persona_simple: str = MODELS["persona_simple"]
    model_persona_complex: str = MODELS["persona_complex"]
    model_analysis: str = MODELS["analysis"]

    # Behavior
    budget: float = DEFAULT_BUDGET_USD
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    headless: bool = True
    timeout: int = 600
    level: str = "standard"

    # Native app / simulator testing
    app_type: str = "web"  # web | native_macos | ios_simulator | api
    app_path: str | None = None  # Path to .app bundle
    bundle_id: str | None = None  # e.g. "com.example.myapp"
    simulator_device: str | None = None  # Simulator UDID or device name
    simulator_os: str | None = None  # e.g. "17.2"

    # Optional integrations (for BusinessAtlas adapter)
    cost_ledger_path: Path | None = None
    system_ledger_path: Path | None = None
    initiative_id: str | None = None

    @classmethod
    def from_file(cls, config_path: Path) -> SpecterQAConfig:
        """Load config from a YAML file."""
        if not config_path.exists():
            raise SpecterQAConfigError(f"Config file not found: {config_path}\n\nTo fix: specterqa init")
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return cls._from_dict(data, config_path.parent)

    @classmethod
    def _from_dict(cls, data: dict[str, Any], project_dir: Path) -> SpecterQAConfig:
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

        # Native app / simulator fields
        if "app_type" in data:
            config.app_type = str(data["app_type"])
        if "app_path" in data:
            config.app_path = str(data["app_path"])
        if "bundle_id" in data:
            config.bundle_id = str(data["bundle_id"])
        if "simulator_device" in data:
            config.simulator_device = str(data["simulator_device"])
        if "simulator_os" in data:
            config.simulator_os = str(data["simulator_os"])

        return config

    def resolve_product(self, product_name: str) -> dict:
        """Load a product config by name."""
        product_path = self.products_dir / f"{product_name}.yaml"
        if not product_path.exists():
            raise SpecterQAConfigError(
                f"Product not found: {product_name}\n\n"
                f"Expected file: {product_path}\n"
                "To fix: Create the product config file"
            )
        with open(product_path) as f:
            return yaml.safe_load(f)
