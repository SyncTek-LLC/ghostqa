"""Unit tests for ghostqa.credentials — API key resolution and masking."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from ghostqa.config import GhostQAConfigError
from ghostqa.credentials import (
    _parse_env_file,
    _parse_yaml_key,
    mask_key,
    resolve_api_key,
)


# ---------------------------------------------------------------------------
# 1. resolve_api_key() — from environment variable
# ---------------------------------------------------------------------------

class TestResolveApiKeyFromEnv:
    """resolve_api_key() should prefer ANTHROPIC_API_KEY env var."""

    def test_returns_env_var_when_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123")
        key = resolve_api_key()
        assert key == "sk-ant-test-key-123"

    def test_env_var_takes_priority_over_other_sources(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Even when .env and config.yaml exist, env var should win."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

        # Create a .env file with a different key (in cwd)
        env_file = tmp_path / ".env"
        env_file.write_text('ANTHROPIC_API_KEY=sk-ant-from-dotenv\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        key = resolve_api_key(project_dir=tmp_path)
        assert key == "sk-ant-from-env"


# ---------------------------------------------------------------------------
# 2. resolve_api_key() — from .env file
# ---------------------------------------------------------------------------

class TestResolveApiKeyFromDotenv:
    """resolve_api_key() should fall back to .env when env var is not set."""

    def test_returns_key_from_dotenv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        env_file = tmp_path / ".env"
        env_file.write_text('ANTHROPIC_API_KEY=sk-ant-dotenv-key\n', encoding="utf-8")

        key = resolve_api_key()
        assert key == "sk-ant-dotenv-key"

    def test_dotenv_strips_quotes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY='sk-ant-quoted'\n", encoding="utf-8")

        key = resolve_api_key()
        assert key == "sk-ant-quoted"


# ---------------------------------------------------------------------------
# 3. resolve_api_key() — from YAML config
# ---------------------------------------------------------------------------

class TestResolveApiKeyFromYaml:
    """resolve_api_key() should fall back to project config.yaml."""

    def test_returns_key_from_project_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        # No .env file in cwd

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            yaml.dump({"anthropic_api_key": "sk-ant-yaml-key"}), encoding="utf-8"
        )

        key = resolve_api_key(project_dir=tmp_path)
        assert key == "sk-ant-yaml-key"

    def test_yaml_supports_api_key_alias(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """_parse_yaml_key() should also look for 'api_key' key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(yaml.dump({"api_key": "sk-ant-alias"}), encoding="utf-8")

        key = resolve_api_key(project_dir=tmp_path)
        assert key == "sk-ant-alias"


# ---------------------------------------------------------------------------
# 4. resolve_api_key() — error when no key found
# ---------------------------------------------------------------------------

class TestResolveApiKeyRaises:
    """resolve_api_key() should raise GhostQAConfigError when no key is found."""

    def test_raises_when_no_key_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        # Mock home to avoid reading real ~/.ghostqa/config.yaml
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")

        with pytest.raises(GhostQAConfigError, match="ANTHROPIC_API_KEY not set"):
            resolve_api_key()


# ---------------------------------------------------------------------------
# 5. mask_key() — normal key
# ---------------------------------------------------------------------------

class TestMaskKey:
    """mask_key() should partially redact API keys for safe display."""

    def test_mask_normal_key(self):
        key = "sk-ant-api03-abcdefghijklmnop"
        masked = mask_key(key)
        assert masked.startswith("sk-ant-")
        assert masked.endswith("nop")
        assert "..." in masked
        # The original full key should not appear
        assert masked != key

    def test_mask_preserves_prefix_and_suffix(self):
        key = "sk-ant-api03-longkeyhere12345"
        masked = mask_key(key)
        assert masked == "sk-ant-...345"

    def test_mask_short_key_returns_stars(self):
        assert mask_key("short") == "***"
        assert mask_key("exactly10c") == "***"

    def test_mask_boundary_11_chars_shows_partial(self):
        key = "12345678901"  # 11 chars
        masked = mask_key(key)
        assert masked == "1234567...901"

    def test_mask_empty_key(self):
        assert mask_key("") == "***"


# ---------------------------------------------------------------------------
# 6. _parse_env_file() — internal helper
# ---------------------------------------------------------------------------

class TestParseEnvFile:
    """_parse_env_file() should correctly extract values from .env files."""

    def test_extracts_key_value(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
        assert _parse_env_file(env_file, "FOO") == "bar"
        assert _parse_env_file(env_file, "BAZ") == "qux"

    def test_ignores_comments(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nKEY=value\n", encoding="utf-8")
        assert _parse_env_file(env_file, "KEY") == "value"

    def test_returns_none_for_missing_key(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=value\n", encoding="utf-8")
        assert _parse_env_file(env_file, "MISSING") is None

    def test_strips_surrounding_quotes(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY='value'\n", encoding="utf-8")
        assert _parse_env_file(env_file, "KEY") == "value"

    def test_handles_double_quotes(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text('KEY="value"\n', encoding="utf-8")
        assert _parse_env_file(env_file, "KEY") == "value"


# ---------------------------------------------------------------------------
# 7. _parse_yaml_key() — internal helper
# ---------------------------------------------------------------------------

class TestParseYamlKey:
    """_parse_yaml_key() should extract API keys from YAML config files."""

    def test_extracts_anthropic_api_key(self, tmp_path: Path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            yaml.dump({"anthropic_api_key": "sk-ant-yaml"}), encoding="utf-8"
        )
        assert _parse_yaml_key(yaml_file) == "sk-ant-yaml"

    def test_extracts_api_key_alias(self, tmp_path: Path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml.dump({"api_key": "sk-ant-alias"}), encoding="utf-8")
        assert _parse_yaml_key(yaml_file) == "sk-ant-alias"

    def test_returns_none_for_missing_keys(self, tmp_path: Path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml.dump({"other": "data"}), encoding="utf-8")
        assert _parse_yaml_key(yaml_file) is None

    def test_returns_none_for_invalid_yaml(self, tmp_path: Path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(":::not_valid_yaml:::", encoding="utf-8")
        # Should not raise, just return None
        assert _parse_yaml_key(yaml_file) is None
