"""Unit tests for specterqa.config — SpecterQAConfig and related functions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from specterqa.config import SpecterQAConfig, SpecterQAConfigError
from specterqa.models import DEFAULT_BUDGET_USD, DEFAULT_VIEWPORT, MODELS


# ---------------------------------------------------------------------------
# 1. Default values
# ---------------------------------------------------------------------------

class TestSpecterQAConfigDefaults:
    """SpecterQAConfig should have sensible defaults for every field."""

    def test_default_product_name_is_empty(self):
        cfg = SpecterQAConfig()
        assert cfg.product_name == ""

    def test_default_base_url_is_empty(self):
        cfg = SpecterQAConfig()
        assert cfg.base_url == ""

    def test_default_budget_matches_models_constant(self):
        cfg = SpecterQAConfig()
        assert cfg.budget == DEFAULT_BUDGET_USD

    def test_default_viewport_matches_models_constant(self):
        cfg = SpecterQAConfig()
        assert cfg.viewport == DEFAULT_VIEWPORT

    def test_default_headless_is_true(self):
        cfg = SpecterQAConfig()
        assert cfg.headless is True

    def test_default_timeout_is_600(self):
        cfg = SpecterQAConfig()
        assert cfg.timeout == 600

    def test_default_model_fields_reference_models_dict(self):
        cfg = SpecterQAConfig()
        assert cfg.model_persona_simple == MODELS["persona_simple"]
        assert cfg.model_persona_complex == MODELS["persona_complex"]
        assert cfg.model_analysis == MODELS["analysis"]

    def test_default_level_is_standard(self):
        cfg = SpecterQAConfig()
        assert cfg.level == "standard"

    def test_optional_integration_fields_are_none(self):
        cfg = SpecterQAConfig()
        assert cfg.cost_ledger_path is None
        assert cfg.system_ledger_path is None
        assert cfg.initiative_id is None
        assert cfg.fixtures_dir is None


# ---------------------------------------------------------------------------
# 2. from_file() — happy path
# ---------------------------------------------------------------------------

class TestFromFile:
    """SpecterQAConfig.from_file() should load and parse valid YAML."""

    def test_from_file_with_valid_yaml(self, tmp_path: Path, sample_config_yaml: str):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(sample_config_yaml, encoding="utf-8")

        cfg = SpecterQAConfig.from_file(config_file)

        assert cfg.budget == 5.00
        assert cfg.headless is True
        assert cfg.timeout == 300
        assert cfg.viewport == (1920, 1080)
        # project_dir should be the parent of the config file
        assert cfg.project_dir == tmp_path

    def test_from_file_missing_file_raises_config_error(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(SpecterQAConfigError, match="Config file not found"):
            SpecterQAConfig.from_file(missing)

    def test_from_file_empty_yaml_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("", encoding="utf-8")

        cfg = SpecterQAConfig.from_file(config_file)
        # All defaults should still hold
        assert cfg.budget == DEFAULT_BUDGET_USD
        assert cfg.headless is True


# ---------------------------------------------------------------------------
# 3. _from_dict() — key mapping
# ---------------------------------------------------------------------------

class TestFromDict:
    """_from_dict() should correctly map YAML keys to config attributes."""

    def test_maps_all_directory_keys(self, tmp_path: Path):
        data = {
            "products_dir": "my_products",
            "personas_dir": "my_personas",
            "journeys_dir": "my_journeys",
            "evidence_dir": "my_evidence",
        }
        cfg = SpecterQAConfig._from_dict(data, tmp_path)
        assert cfg.products_dir == tmp_path / "my_products"
        assert cfg.personas_dir == tmp_path / "my_personas"
        assert cfg.journeys_dir == tmp_path / "my_journeys"
        assert cfg.evidence_dir == tmp_path / "my_evidence"

    def test_default_dirs_when_keys_missing(self, tmp_path: Path):
        cfg = SpecterQAConfig._from_dict({}, tmp_path)
        assert cfg.products_dir == tmp_path / "products"
        assert cfg.personas_dir == tmp_path / "personas"
        assert cfg.journeys_dir == tmp_path / "journeys"
        assert cfg.evidence_dir == tmp_path / "evidence"


# ---------------------------------------------------------------------------
# 4. Viewport dict parsing
# ---------------------------------------------------------------------------

class TestViewportParsing:
    """Viewport should be parsed from a dict with width/height keys."""

    def test_viewport_dict_parsed_correctly(self, tmp_path: Path):
        data = {"viewport": {"width": 800, "height": 600}}
        cfg = SpecterQAConfig._from_dict(data, tmp_path)
        assert cfg.viewport == (800, 600)

    def test_viewport_dict_partial_keys_use_defaults(self, tmp_path: Path):
        data = {"viewport": {"width": 1920}}
        cfg = SpecterQAConfig._from_dict(data, tmp_path)
        assert cfg.viewport == (1920, 720)

    def test_viewport_non_dict_is_ignored(self, tmp_path: Path):
        data = {"viewport": "1280x720"}
        cfg = SpecterQAConfig._from_dict(data, tmp_path)
        # Should remain the dataclass default since it's not a dict
        assert cfg.viewport == DEFAULT_VIEWPORT


# ---------------------------------------------------------------------------
# 5. Budget / timeout / headless parsing
# ---------------------------------------------------------------------------

class TestBehaviorParsing:
    """Budget, timeout, and headless should be parsed from YAML data."""

    def test_budget_parsed_as_float(self, tmp_path: Path):
        cfg = SpecterQAConfig._from_dict({"budget": "10.50"}, tmp_path)
        assert cfg.budget == 10.50

    def test_timeout_parsed_as_int(self, tmp_path: Path):
        cfg = SpecterQAConfig._from_dict({"timeout": "120"}, tmp_path)
        assert cfg.timeout == 120

    def test_headless_parsed_as_bool(self, tmp_path: Path):
        cfg = SpecterQAConfig._from_dict({"headless": False}, tmp_path)
        assert cfg.headless is False


# ---------------------------------------------------------------------------
# 6. resolve_product()
# ---------------------------------------------------------------------------

class TestResolveProduct:
    """resolve_product() should load product YAML or raise on missing."""

    def test_resolve_product_valid(self, tmp_project_dir: Path, sample_product_yaml: str):
        products_dir = tmp_project_dir / "products"
        (products_dir / "testapp.yaml").write_text(sample_product_yaml, encoding="utf-8")

        cfg = SpecterQAConfig()
        cfg.products_dir = products_dir

        product = cfg.resolve_product("testapp")
        assert product["product"]["name"] == "testapp"
        assert product["product"]["base_url"] == "http://localhost:3000"

    def test_resolve_product_missing_raises(self, tmp_project_dir: Path):
        cfg = SpecterQAConfig()
        cfg.products_dir = tmp_project_dir / "products"

        with pytest.raises(SpecterQAConfigError, match="Product not found"):
            cfg.resolve_product("nonexistent")
