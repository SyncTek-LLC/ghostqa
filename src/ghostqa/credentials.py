"""API key resolution for GhostQA."""

from __future__ import annotations

import os
from pathlib import Path

from ghostqa.config import GhostQAConfigError


def resolve_api_key(project_dir: Path | None = None) -> str:
    """Resolve Anthropic API key from multiple sources.

    Resolution order (highest priority first):
    1. ANTHROPIC_API_KEY environment variable
    2. .env file in current directory
    3. Project config (.ghostqa/config.yaml)
    4. Global config (~/.ghostqa/config.yaml)
    """
    # 1. Environment variable
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key

    # 2. .env file
    env_path = Path(".env")
    if env_path.exists():
        key = _parse_env_file(env_path, "ANTHROPIC_API_KEY")
        if key:
            return key

    # 3. Project config
    if project_dir:
        config_path = project_dir / "config.yaml"
        if config_path.exists():
            key = _parse_yaml_key(config_path)
            if key:
                return key

    # 4. Global config
    global_config = Path.home() / ".ghostqa" / "config.yaml"
    if global_config.exists():
        key = _parse_yaml_key(global_config)
        if key:
            return key

    # 5. Error with clear instructions
    raise GhostQAConfigError(
        "ANTHROPIC_API_KEY not set\n\n"
        "GhostQA needs an Anthropic API key to run persona simulations.\n\n"
        "To fix:\n"
        "  export ANTHROPIC_API_KEY=sk-ant-your-key-here\n"
        "  or: ghostqa config set-key anthropic"
    )


def mask_key(key: str) -> str:
    """Mask an API key for display. Shows first 7 and last 3 chars."""
    if len(key) <= 10:
        return "***"
    return f"{key[:7]}...{key[-3:]}"


def _parse_env_file(path: Path, key_name: str) -> str | None:
    """Parse a .env file for a specific key."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key_name:
                    return v.strip().strip("'\"")
    except Exception:
        pass
    return None


def _parse_yaml_key(path: Path) -> str | None:
    """Parse a YAML config file for an API key."""
    try:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("anthropic_api_key") or data.get("api_key")
    except Exception:
        pass
    return None
