"""Unit and component tests for ghostqa.mcp.server.

Covers all pure helper functions and all four MCP tool handlers.
Tool handlers are invoked via create_server() + mcp.call_tool() to test
the full dispatch path without a real browser, AI API, or Playwright install.

The MCP server contains a directory allowlist (FIND-002 security control).
To bypass it during tests, tools that use directory access are invoked
with the GHOSTQA_ALLOWED_DIRS env var patched to include tmp_path, and
_ALLOWED_DIRS is reloaded for each test via a fixture.

Test structure:
    TestBuildAllowedDirs        — _build_allowed_dirs()
    TestValidateDirectory       — _validate_directory()
    TestResolveProjectDir       — _resolve_project_dir()
    TestCheckProjectInitialized — _check_project_initialized()
    TestCheckPlaywrightAvailable — _check_playwright_available()
    TestJsonSerialize           — _json_serialize()
    TestListAllRunIds           — _list_all_run_ids()
    TestLoadRunResult           — _load_run_result()
    TestFindJourneysForProduct  — _find_journeys_for_product()
    TestBuildConfig             — _build_config()
    TestGhostqaInitTool         — ghostqa_init MCP tool
    TestGhostqaListProductsTool — ghostqa_list_products MCP tool
    TestGhostqaGetResultsTool   — ghostqa_get_results MCP tool
    TestGhostqaRunTool          — ghostqa_run MCP tool (error paths + mocked runs)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_result(content_list: list) -> Any:
    """Extract and parse the JSON text from the first TextContent in a call_tool result."""
    return json.loads(content_list[0].text)


def run_async(coro):
    """Run a coroutine synchronously in a new event loop."""
    return asyncio.run(coro)


def _make_server_with_allowed(tmp_path: Path):
    """
    Create an MCP server.

    The allowlist (_ALLOWED_DIRS) defaults to None when GHOSTQA_ALLOWED_DIRS
    is not set, meaning no directory restriction is active. Tests for the
    full tool dispatch use this helper so all directories are allowed.

    Returns (mcp, srv_module, original_allowed) for test cleanup convenience.
    The original value is stored but restoration is a no-op in the common case.
    """
    import ghostqa.mcp.server as srv_module
    from ghostqa.mcp.server import create_server

    original = srv_module._ALLOWED_DIRS
    # Ensure no restriction is active for general tool tests
    srv_module._ALLOWED_DIRS = None
    mcp = create_server()
    return mcp, srv_module, original


def _restore_allowed(srv_module, original):
    """Restore the original _ALLOWED_DIRS after a test."""
    srv_module._ALLOWED_DIRS = original


def _make_initialized_project(tmp_path: Path) -> Path:
    """Create a minimal .ghostqa/ directory with all standard subdirs."""
    ghostqa_dir = tmp_path / ".ghostqa"
    for sub in ("products", "personas", "journeys", "evidence"):
        (ghostqa_dir / sub).mkdir(parents=True)
    return ghostqa_dir


# ---------------------------------------------------------------------------
# 0. _build_allowed_dirs() and _validate_directory()
# ---------------------------------------------------------------------------

class TestBuildAllowedDirs:
    """_build_allowed_dirs() reads GHOSTQA_ALLOWED_DIRS.

    When not set (or set to whitespace), returns None — meaning no restriction.
    When set, returns a list of resolved Path objects.
    """

    def test_returns_none_when_env_not_set(self):
        from ghostqa.mcp.server import _build_allowed_dirs

        with patch.dict(os.environ, {"GHOSTQA_ALLOWED_DIRS": ""}):
            result = _build_allowed_dirs()

        assert result is None

    def test_returns_none_when_env_is_whitespace(self):
        from ghostqa.mcp.server import _build_allowed_dirs

        with patch.dict(os.environ, {"GHOSTQA_ALLOWED_DIRS": "   "}):
            result = _build_allowed_dirs()

        assert result is None

    def test_parses_single_path_from_env(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_allowed_dirs

        with patch.dict(os.environ, {"GHOSTQA_ALLOWED_DIRS": str(tmp_path)}):
            result = _build_allowed_dirs()

        assert result is not None
        assert len(result) == 1
        assert result[0] == tmp_path.resolve()

    def test_parses_colon_separated_paths(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_allowed_dirs

        p1 = tmp_path / "a"
        p2 = tmp_path / "b"
        p1.mkdir()
        p2.mkdir()
        env_val = f"{p1}:{p2}"
        with patch.dict(os.environ, {"GHOSTQA_ALLOWED_DIRS": env_val}):
            result = _build_allowed_dirs()

        assert result is not None
        assert len(result) == 2
        assert p1.resolve() in result
        assert p2.resolve() in result

    def test_ignores_blank_entries_in_colon_list(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_allowed_dirs

        with patch.dict(os.environ, {"GHOSTQA_ALLOWED_DIRS": f"{tmp_path}::"}):
            result = _build_allowed_dirs()

        assert result is not None
        assert len(result) == 1


class TestValidateDirectory:
    """_validate_directory() enforces the allowlist when configured.

    _ALLOWED_DIRS = None means no restriction (all directories allowed).
    _ALLOWED_DIRS = [list] means only paths within the list are permitted.
    """

    def test_allows_any_path_when_no_restriction(self, tmp_path: Path):
        """When _ALLOWED_DIRS is None, all directories are allowed."""
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = None
            result_path, err = _validate_directory(str(tmp_path))
            assert err is None
            assert result_path == tmp_path.resolve()
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_allows_path_within_allowed_dir(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
            result_path, err = _validate_directory(str(tmp_path))
            assert err is None
            assert result_path == tmp_path.resolve()
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_allows_subdirectory_within_allowed_dir(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        subdir = tmp_path / "sub" / "deeper"
        subdir.mkdir(parents=True)
        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
            result_path, err = _validate_directory(str(subdir))
            assert err is None
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_rejects_path_outside_allowed_dir(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
            result_path, err = _validate_directory("/etc")
            assert result_path is None
            assert err is not None
            assert "access denied" in err.lower()
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_error_message_includes_denied_path(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
            _, err = _validate_directory("/usr/local")
            assert "/usr/local" in err or "usr" in err
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_error_message_includes_allowed_list(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
            _, err = _validate_directory("/etc")
            assert str(tmp_path) in err
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_none_directory_uses_cwd_when_no_restriction(self):
        """Passing directory=None should resolve to CWD and succeed when unrestricted."""
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import _validate_directory

        cwd = Path.cwd().resolve()
        original = srv_module._ALLOWED_DIRS
        try:
            srv_module._ALLOWED_DIRS = None
            result_path, err = _validate_directory(None)
            assert err is None
            assert result_path == cwd
        finally:
            srv_module._ALLOWED_DIRS = original


# ---------------------------------------------------------------------------
# 1. _resolve_project_dir()
# ---------------------------------------------------------------------------

class TestResolveProjectDir:
    """_resolve_project_dir() should locate .ghostqa/ by walking up from start."""

    def test_finds_ghostqa_in_start_directory(self, tmp_path: Path):
        from ghostqa.mcp.server import _resolve_project_dir

        ghostqa_dir = tmp_path / ".ghostqa"
        ghostqa_dir.mkdir()

        result = _resolve_project_dir(str(tmp_path))
        assert result == ghostqa_dir

    def test_finds_ghostqa_in_parent_directory(self, tmp_path: Path):
        from ghostqa.mcp.server import _resolve_project_dir

        ghostqa_dir = tmp_path / ".ghostqa"
        ghostqa_dir.mkdir()
        deep_child = tmp_path / "subdir" / "deeper"
        deep_child.mkdir(parents=True)

        result = _resolve_project_dir(str(deep_child))
        assert result == ghostqa_dir

    def test_returns_default_when_not_found(self, tmp_path: Path):
        from ghostqa.mcp.server import _resolve_project_dir

        isolated = tmp_path / "isolated"
        isolated.mkdir()

        result = _resolve_project_dir(str(isolated))
        assert result == isolated / ".ghostqa"

    def test_defaults_to_cwd_when_directory_is_none(self):
        from ghostqa.mcp.server import _resolve_project_dir

        result = _resolve_project_dir(None)
        assert isinstance(result, Path)
        assert result.name == ".ghostqa"

    def test_finds_ghostqa_exactly_one_level_up(self, tmp_path: Path):
        from ghostqa.mcp.server import _resolve_project_dir

        ghostqa_dir = tmp_path / ".ghostqa"
        ghostqa_dir.mkdir()
        child = tmp_path / "child"
        child.mkdir()

        result = _resolve_project_dir(str(child))
        assert result == ghostqa_dir

    def test_prefers_nearer_ghostqa_over_distant_ancestor(self, tmp_path: Path):
        from ghostqa.mcp.server import _resolve_project_dir

        (tmp_path / ".ghostqa").mkdir()
        sub = tmp_path / "sub"
        sub.mkdir()
        sub_ghostqa = sub / ".ghostqa"
        sub_ghostqa.mkdir()

        result = _resolve_project_dir(str(sub))
        assert result == sub_ghostqa


# ---------------------------------------------------------------------------
# 2. _check_project_initialized()
# ---------------------------------------------------------------------------

class TestCheckProjectInitialized:
    """_check_project_initialized() returns None on init, error string otherwise."""

    def test_returns_none_for_existing_project_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _check_project_initialized

        project_dir = tmp_path / ".ghostqa"
        project_dir.mkdir()
        assert _check_project_initialized(project_dir) is None

    def test_returns_error_string_for_missing_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _check_project_initialized

        project_dir = tmp_path / ".ghostqa"
        result = _check_project_initialized(project_dir)
        assert result is not None
        assert isinstance(result, str)
        assert "not initialized" in result

    def test_error_message_contains_parent_path(self, tmp_path: Path):
        from ghostqa.mcp.server import _check_project_initialized

        project_dir = tmp_path / ".ghostqa"
        result = _check_project_initialized(project_dir)
        assert str(tmp_path) in result

    def test_error_message_contains_init_hint(self, tmp_path: Path):
        from ghostqa.mcp.server import _check_project_initialized

        project_dir = tmp_path / ".ghostqa"
        result = _check_project_initialized(project_dir)
        assert "ghostqa_init" in result or "ghostqa init" in result

    def test_returns_none_for_nested_project_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _check_project_initialized

        nested = tmp_path / "a" / "b" / ".ghostqa"
        nested.mkdir(parents=True)
        assert _check_project_initialized(nested) is None


# ---------------------------------------------------------------------------
# 3. _check_playwright_available()
# ---------------------------------------------------------------------------

class TestCheckPlaywrightAvailable:
    """_check_playwright_available() returns None when importable, error when not."""

    def test_returns_none_when_playwright_importable(self):
        from ghostqa.mcp.server import _check_playwright_available

        result = _check_playwright_available()
        assert result is None

    def test_returns_error_when_playwright_not_importable(self):
        from ghostqa.mcp.server import _check_playwright_available

        with patch.dict(sys.modules, {"playwright": None}):
            result = _check_playwright_available()

        assert result is not None
        assert "Playwright" in result
        assert "pip install playwright" in result

    def test_error_message_includes_install_instructions(self):
        from ghostqa.mcp.server import _check_playwright_available

        with patch.dict(sys.modules, {"playwright": None}):
            result = _check_playwright_available()

        assert "playwright install chromium" in result


# ---------------------------------------------------------------------------
# 4. _json_serialize()
# ---------------------------------------------------------------------------

class TestJsonSerialize:
    """_json_serialize() is used as default= in json.dumps for Path objects."""

    def test_serializes_path_to_string(self, tmp_path: Path):
        from ghostqa.mcp.server import _json_serialize

        p = tmp_path / "some" / "file.txt"
        assert _json_serialize(p) == str(p)

    def test_serializes_path_preserves_separator(self):
        from ghostqa.mcp.server import _json_serialize

        p = Path("/usr/local/bin/ghostqa")
        assert _json_serialize(p) == "/usr/local/bin/ghostqa"

    def test_raises_type_error_for_unknown_type(self):
        from ghostqa.mcp.server import _json_serialize

        with pytest.raises(TypeError, match="not JSON serializable"):
            _json_serialize(object())

    def test_raises_for_set(self):
        from ghostqa.mcp.server import _json_serialize

        with pytest.raises(TypeError):
            _json_serialize({1, 2, 3})

    def test_used_correctly_via_json_dumps(self, tmp_path: Path):
        from ghostqa.mcp.server import _json_serialize

        data = {"path": tmp_path}
        output = json.dumps(data, default=_json_serialize)
        parsed = json.loads(output)
        assert parsed["path"] == str(tmp_path)


# ---------------------------------------------------------------------------
# 5. _list_all_run_ids()
# ---------------------------------------------------------------------------

class TestListAllRunIds:
    """_list_all_run_ids() lists run dirs from evidence directory, most recent first."""

    def test_returns_empty_list_for_nonexistent_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        missing = tmp_path / "evidence"
        assert _list_all_run_ids(missing) == []

    def test_returns_empty_list_for_empty_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        assert _list_all_run_ids(evidence) == []

    def test_lists_gqa_run_dirs_sorted_descending(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        for name in ["GQA-RUN-20260101-100000-aaaa", "GQA-RUN-20260102-100000-bbbb", "GQA-RUN-20260103-100000-cccc"]:
            (evidence / name).mkdir()

        result = _list_all_run_ids(evidence)
        assert result == [
            "GQA-RUN-20260103-100000-cccc",
            "GQA-RUN-20260102-100000-bbbb",
            "GQA-RUN-20260101-100000-aaaa",
        ]

    def test_ignores_non_run_directories(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "GQA-RUN-20260101-100000-aaaa").mkdir()
        (evidence / "some-other-dir").mkdir()
        (evidence / "reports").mkdir()
        (evidence / "GQA-RUN-20260102-100000-bbbb").mkdir()

        result = _list_all_run_ids(evidence)
        assert "some-other-dir" not in result
        assert "reports" not in result
        assert len(result) == 2

    def test_ignores_files_in_evidence_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "GQA-RUN-20260101-100000-aaaa").mkdir()
        (evidence / "GQA-RUN-20260101-100000-bbbb.txt").write_text("not a dir")

        result = _list_all_run_ids(evidence)
        assert result == ["GQA-RUN-20260101-100000-aaaa"]

    def test_single_run_returns_list_of_one(self, tmp_path: Path):
        from ghostqa.mcp.server import _list_all_run_ids

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "GQA-RUN-20260201-123456-a1b2").mkdir()

        result = _list_all_run_ids(evidence)
        assert result == ["GQA-RUN-20260201-123456-a1b2"]


# ---------------------------------------------------------------------------
# 6. _load_run_result()
# ---------------------------------------------------------------------------

class TestLoadRunResult:
    """_load_run_result() reads run-result.json from an evidence directory."""

    def test_returns_none_for_missing_run_id(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        evidence.mkdir()
        assert _load_run_result(evidence, "GQA-RUN-20260101-000000-xxxx") is None

    def test_returns_none_for_missing_result_file(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        run_dir = evidence / "GQA-RUN-20260101-000000-aaaa"
        run_dir.mkdir(parents=True)

        assert _load_run_result(evidence, "GQA-RUN-20260101-000000-aaaa") is None

    def test_returns_dict_for_valid_json(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        run_id = "GQA-RUN-20260101-120000-a1b2"
        run_dir = evidence / run_id
        run_dir.mkdir(parents=True)
        payload = {"run_id": run_id, "passed": True, "cost_usd": 0.42}
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")

        result = _load_run_result(evidence, run_id)
        assert result == payload
        assert result["passed"] is True
        assert result["cost_usd"] == 0.42

    def test_returns_none_for_invalid_json(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        run_id = "GQA-RUN-20260101-130000-bad1"
        run_dir = evidence / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run-result.json").write_text("NOT VALID JSON {{{", encoding="utf-8")

        assert _load_run_result(evidence, run_id) is None

    def test_returns_none_for_empty_file(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        run_id = "GQA-RUN-20260101-140000-empty"
        run_dir = evidence / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run-result.json").write_text("", encoding="utf-8")

        assert _load_run_result(evidence, run_id) is None

    def test_loads_nested_structure(self, tmp_path: Path):
        from ghostqa.mcp.server import _load_run_result

        evidence = tmp_path / "evidence"
        run_id = "GQA-RUN-20260101-150000-nest"
        run_dir = evidence / run_id
        run_dir.mkdir(parents=True)
        payload = {
            "run_id": run_id,
            "passed": False,
            "step_reports": [
                {"step_id": "step-1", "passed": True},
                {"step_id": "step-2", "passed": False},
            ],
            "findings": [{"severity": "high", "description": "Login broken"}],
            "cost_usd": 1.23,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")

        result = _load_run_result(evidence, run_id)
        assert len(result["step_reports"]) == 2
        assert result["findings"][0]["severity"] == "high"


# ---------------------------------------------------------------------------
# 7. _find_journeys_for_product()
# ---------------------------------------------------------------------------

class TestFindJourneysForProduct:
    """_find_journeys_for_product() scans global and product-scoped journey YAML files."""

    def test_returns_empty_list_when_no_journeys_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = tmp_path / ".ghostqa"
        project_dir.mkdir()
        assert _find_journeys_for_product(project_dir, "myapp") == []

    def test_returns_empty_list_for_empty_journeys_dir(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        assert _find_journeys_for_product(project_dir, "myapp") == []

    def test_finds_journey_with_scenario_wrapper(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        journey_data = {
            "scenario": {
                "id": "onboarding",
                "name": "User Onboarding",
                "tags": ["smoke", "auth"],
            }
        }
        (project_dir / "journeys" / "onboarding.yaml").write_text(
            yaml.dump(journey_data), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        assert len(result) == 1
        assert result[0]["id"] == "onboarding"
        assert result[0]["name"] == "User Onboarding"
        assert result[0]["tags"] == ["smoke", "auth"]

    def test_finds_journey_without_scenario_wrapper(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        journey_data = {"id": "signup", "name": "Sign Up Flow"}
        (project_dir / "journeys" / "signup.yaml").write_text(
            yaml.dump(journey_data), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        assert len(result) == 1
        assert result[0]["id"] == "signup"

    def test_uses_file_stem_as_id_fallback(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        (project_dir / "journeys" / "my-journey.yaml").write_text(
            yaml.dump({"name": "Some Journey"}), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        assert result[0]["id"] == "my-journey"

    def test_finds_multiple_journeys_sorted(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        for name in ["b-journey", "a-journey", "c-journey"]:
            data = {"scenario": {"id": name, "name": name.title()}}
            (project_dir / "journeys" / f"{name}.yaml").write_text(
                yaml.dump(data), encoding="utf-8"
            )

        result = _find_journeys_for_product(project_dir, "myapp")
        ids = [j["id"] for j in result]
        assert ids == sorted(ids)

    def test_finds_product_scoped_journeys(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        product_journeys = project_dir / "myapp" / "journeys"
        product_journeys.mkdir(parents=True)
        scoped_data = {"scenario": {"id": "scoped-journey", "name": "Scoped"}}
        (product_journeys / "scoped.yaml").write_text(
            yaml.dump(scoped_data), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        ids = [j["id"] for j in result]
        assert "scoped-journey" in ids

    def test_deduplicates_journeys_with_same_id(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        shared_data = {"scenario": {"id": "shared-id", "name": "Shared"}}
        (project_dir / "journeys" / "shared.yaml").write_text(
            yaml.dump(shared_data), encoding="utf-8"
        )
        product_journeys = project_dir / "myapp" / "journeys"
        product_journeys.mkdir(parents=True)
        (product_journeys / "shared.yaml").write_text(
            yaml.dump(shared_data), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        ids = [j["id"] for j in result]
        assert ids.count("shared-id") == 1

    def test_skips_invalid_yaml_files(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        (project_dir / "journeys" / "valid.yaml").write_text(
            yaml.dump({"scenario": {"id": "valid", "name": "Valid"}}), encoding="utf-8"
        )
        (project_dir / "journeys" / "corrupt.yaml").write_text(
            "{{{{ INVALID YAML ::::", encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        ids = [j["id"] for j in result]
        assert "valid" in ids

    def test_tags_default_to_empty_list(self, tmp_path: Path):
        from ghostqa.mcp.server import _find_journeys_for_product

        project_dir = _make_initialized_project(tmp_path)
        (project_dir / "journeys" / "notags.yaml").write_text(
            yaml.dump({"scenario": {"id": "notags", "name": "No Tags"}}), encoding="utf-8"
        )

        result = _find_journeys_for_product(project_dir, "myapp")
        assert result[0]["tags"] == []


# ---------------------------------------------------------------------------
# 8. _build_config()
# ---------------------------------------------------------------------------

class TestBuildConfig:
    """_build_config() produces a GhostQAConfig from the project directory."""

    def test_builds_config_without_yaml_file(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config = _build_config(project_dir)
        assert config is not None
        assert config.project_dir == project_dir

    def test_builds_config_with_yaml_file(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config_data = {"budget": 10.0, "headless": False, "timeout": 300}
        (project_dir / "config.yaml").write_text(yaml.dump(config_data), encoding="utf-8")

        config = _build_config(project_dir, budget=10.0)
        assert config.budget == 10.0

    def test_budget_parameter_overrides_config(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        (project_dir / "config.yaml").write_text(yaml.dump({"budget": 5.0}), encoding="utf-8")

        config = _build_config(project_dir, budget=99.0)
        assert config.budget == 99.0

    def test_headless_parameter_applied(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config = _build_config(project_dir, headless=False)
        assert config.headless is False

    def test_level_parameter_applied(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config = _build_config(project_dir, level="smoke")
        assert config.level == "smoke"

    def test_default_level_is_standard(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config = _build_config(project_dir)
        assert config.level == "standard"

    def test_sets_directory_paths_when_no_config_yaml(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        config = _build_config(project_dir)
        assert config.products_dir == project_dir / "products"
        assert config.evidence_dir == project_dir / "evidence"

    def test_no_budget_parameter_does_not_override(self, tmp_path: Path):
        from ghostqa.mcp.server import _build_config

        project_dir = _make_initialized_project(tmp_path)
        (project_dir / "config.yaml").write_text(yaml.dump({"budget": 7.5}), encoding="utf-8")

        config = _build_config(project_dir, budget=None)
        assert config.budget == 7.5


# ---------------------------------------------------------------------------
# 9. ghostqa_init MCP tool
# ---------------------------------------------------------------------------

class TestGhostqaInitTool:
    """ghostqa_init tool creates .ghostqa/ directory structure."""

    def test_creates_success_response(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert data["success"] is True
            assert "project_dir" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_ghostqa_directory(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            assert (tmp_path / ".ghostqa").is_dir()
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_all_subdirectories(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            ghostqa_dir = tmp_path / ".ghostqa"
            for sub in ("products", "personas", "journeys", "evidence"):
                assert (ghostqa_dir / sub).is_dir(), f"Missing subdirectory: {sub}/"
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_config_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            config_path = tmp_path / ".ghostqa" / "config.yaml"
            assert config_path.is_file()
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_sample_product_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            assert (tmp_path / ".ghostqa" / "products" / "demo.yaml").is_file()
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_sample_persona_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            assert (tmp_path / ".ghostqa" / "personas" / "alex-developer.yaml").is_file()
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_sample_journey_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            assert (tmp_path / ".ghostqa" / "journeys" / "demo-onboarding.yaml").is_file()
        finally:
            _restore_allowed(srv_module, original)

    def test_creates_gitignore(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            gitignore = tmp_path / ".gitignore"
            assert gitignore.is_file()
            content = gitignore.read_text(encoding="utf-8")
            assert ".ghostqa/personas/" in content
        finally:
            _restore_allowed(srv_module, original)

    def test_appends_to_existing_gitignore(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            existing_gitignore = tmp_path / ".gitignore"
            existing_gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")

            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            run_async(_run())
            content = existing_gitignore.read_text(encoding="utf-8")
            assert "node_modules/" in content
            assert ".env" in content
            assert ".ghostqa/personas/" in content
        finally:
            _restore_allowed(srv_module, original)

    def test_files_created_list_in_response(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert "files_created" in data
            assert len(data["files_created"]) > 0
        finally:
            _restore_allowed(srv_module, original)

    def test_next_steps_in_response(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert "next_steps" in data
            assert len(data["next_steps"]) > 0
        finally:
            _restore_allowed(srv_module, original)

    def test_url_parameter_patches_product_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        custom_url = "https://myapp.example.com"
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path), "url": custom_url})

            run_async(_run())
            product_path = tmp_path / ".ghostqa" / "products" / "demo.yaml"
            content = product_path.read_text(encoding="utf-8")
            assert custom_url in content
        finally:
            _restore_allowed(srv_module, original)

    def test_url_parameter_mentioned_in_next_steps(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        custom_url = "https://myapp.example.com"
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path), "url": custom_url})
                return _json_result(result)

            data = run_async(_run())
            next_steps_text = " ".join(data["next_steps"])
            assert custom_url in next_steps_text
        finally:
            _restore_allowed(srv_module, original)

    def test_error_when_already_initialized(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _first():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})

            async def _second():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            run_async(_first())
            data = run_async(_second())
            assert data["success"] is False
            assert "error" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_error_message_has_hint_to_delete(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert "hint" in data
            assert "Delete" in data["hint"] or "delete" in data["hint"]
        finally:
            _restore_allowed(srv_module, original)

    def test_project_dir_in_success_response(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert ".ghostqa" in data["project_dir"]
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_disallowed_directory(self, tmp_path: Path):
        """ghostqa_init should return an error if the directory is not in the allowlist."""
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import create_server

        original = srv_module._ALLOWED_DIRS
        # Set a restrictive allowlist to trigger rejection
        srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
        mcp = create_server()
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_init", {"directory": "/etc"})
                return _json_result(result)

            data = run_async(_run())
            assert data["success"] is False
            assert "error" in data
            assert "access denied" in data["error"].lower()
        finally:
            srv_module._ALLOWED_DIRS = original


# ---------------------------------------------------------------------------
# 10. ghostqa_list_products MCP tool
# ---------------------------------------------------------------------------

class TestGhostqaListProductsTool:
    """ghostqa_list_products tool returns product list from .ghostqa/products/."""

    def test_error_for_uninitialized_project(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_returns_empty_list_for_no_products(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert isinstance(data, list)
            assert len(data) == 0
        finally:
            _restore_allowed(srv_module, original)

    def test_returns_product_from_flat_yaml(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_data = {
            "product": {
                "name": "MyApp",
                "base_url": "http://localhost:4000",
                "app_type": "web",
            }
        }
        (ghostqa_dir / "products" / "myapp.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["name"] == "MyApp"
            assert data[0]["base_url"] == "http://localhost:4000"
        finally:
            _restore_allowed(srv_module, original)

    def test_extracts_url_from_services_frontend(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_data = {
            "product": {
                "name": "ServiceApp",
                "services": {"frontend": {"url": "http://localhost:8080"}},
                "app_type": "web",
            }
        }
        (ghostqa_dir / "products" / "serviceapp.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert data[0]["base_url"] == "http://localhost:8080"
        finally:
            _restore_allowed(srv_module, original)

    def test_includes_journeys_field(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_data = {"product": {"name": "AppWithJourneys", "base_url": "http://localhost:3000"}}
        (ghostqa_dir / "products" / "app.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert "journeys" in data[0]
            assert isinstance(data[0]["journeys"], list)
        finally:
            _restore_allowed(srv_module, original)

    def test_includes_journeys_when_present(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_data = {"product": {"name": "JourneyApp", "base_url": "http://localhost:3000"}}
        (ghostqa_dir / "products" / "journeyapp.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        journey_data = {"scenario": {"id": "login-flow", "name": "Login Flow", "tags": ["smoke"]}}
        (ghostqa_dir / "journeys" / "login.yaml").write_text(yaml.dump(journey_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            journeys = data[0]["journeys"]
            assert len(journeys) == 1
            assert journeys[0]["id"] == "login-flow"
        finally:
            _restore_allowed(srv_module, original)

    def test_handles_multiple_products(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        for name in ["app-a", "app-b", "app-c"]:
            product_data = {"product": {"name": name, "base_url": "http://localhost"}}
            (ghostqa_dir / "products" / f"{name}.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert len(data) == 3
        finally:
            _restore_allowed(srv_module, original)

    def test_defaults_app_type_to_web(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_data = {"product": {"name": "SimpleApp"}}
        (ghostqa_dir / "products" / "simple.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert data[0]["app_type"] == "web"
        finally:
            _restore_allowed(srv_module, original)

    def test_finds_directory_style_products(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        product_subdir = ghostqa_dir / "products" / "dir-product"
        product_subdir.mkdir()
        product_data = {"product": {"name": "DirProduct", "base_url": "http://localhost:9000"}}
        (product_subdir / "_product.yaml").write_text(yaml.dump(product_data), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            names = [p["name"] for p in data]
            assert "DirProduct" in names
        finally:
            _restore_allowed(srv_module, original)

    def test_skips_invalid_product_yaml_without_crashing(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        valid_data = {"product": {"name": "ValidApp", "base_url": "http://localhost:3000"}}
        (ghostqa_dir / "products" / "valid.yaml").write_text(yaml.dump(valid_data), encoding="utf-8")
        (ghostqa_dir / "products" / "corrupt.yaml").write_text("{{{{ INVALID", encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": str(tmp_path)})
                return _json_result(result)

            data = run_async(_run())
            assert isinstance(data, list)
            names = [p["name"] for p in data]
            assert "ValidApp" in names
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_disallowed_directory(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import create_server

        original = srv_module._ALLOWED_DIRS
        srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
        mcp = create_server()
        try:
            async def _run():
                result, _ = await mcp.call_tool("ghostqa_list_products", {"directory": "/etc"})
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "access denied" in data["error"].lower()
        finally:
            srv_module._ALLOWED_DIRS = original


# ---------------------------------------------------------------------------
# 11. ghostqa_get_results MCP tool
# ---------------------------------------------------------------------------

class TestGhostqaGetResultsTool:
    """ghostqa_get_results tool retrieves stored run results by run ID."""

    def test_error_for_uninitialized_project(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-000000-xxxx", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_error_when_evidence_dir_missing(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = tmp_path / ".ghostqa"
        ghostqa_dir.mkdir()  # No evidence/ subdir
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-000000-xxxx", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_unknown_run_id(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-999999-unkn", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "Run ID not found" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_lists_available_run_ids_in_error(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        existing_run = "GQA-RUN-20260201-100000-real"
        (evidence_dir / existing_run).mkdir()
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-999999-fake", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "available_run_ids" in data
            assert existing_run in data["available_run_ids"]
        finally:
            _restore_allowed(srv_module, original)

    def test_empty_available_run_ids_when_no_runs(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-999999-fake", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert data["available_run_ids"] == []
            assert "hint" in data
        finally:
            _restore_allowed(srv_module, original)

    def test_returns_run_result_for_valid_run_id(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        run_id = "GQA-RUN-20260201-143052-a1b2"
        run_dir = evidence_dir / run_id
        run_dir.mkdir()
        payload = {
            "run_id": run_id,
            "passed": True,
            "step_reports": [{"step_id": "step-1", "passed": True}],
            "findings": [],
            "cost_usd": 0.35,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": run_id, "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert data["run_id"] == run_id
            assert data["passed"] is True
            assert data["cost_usd"] == 0.35
        finally:
            _restore_allowed(srv_module, original)

    def test_result_contains_step_reports(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        run_id = "GQA-RUN-20260202-100000-step"
        run_dir = evidence_dir / run_id
        run_dir.mkdir()
        payload = {
            "run_id": run_id,
            "passed": False,
            "step_reports": [
                {"step_id": "step-1", "passed": True},
                {"step_id": "step-2", "passed": False},
            ],
            "findings": [{"severity": "high", "description": "Submit button missing"}],
            "cost_usd": 1.10,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": run_id, "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert len(data["step_reports"]) == 2
            assert len(data["findings"]) == 1
            assert data["findings"][0]["severity"] == "high"
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_disallowed_directory(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import create_server

        original = srv_module._ALLOWED_DIRS
        srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
        mcp = create_server()
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_get_results",
                    {"run_id": "GQA-RUN-20260101-000000-xxxx", "directory": "/etc"},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "access denied" in data["error"].lower()
        finally:
            srv_module._ALLOWED_DIRS = original


# ---------------------------------------------------------------------------
# 12. ghostqa_run MCP tool — error paths and mocked runs
# ---------------------------------------------------------------------------

class TestGhostqaRunTool:
    """ghostqa_run tool — test error paths and guard checks."""

    def test_error_for_invalid_level(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_run",
                    {"product": "myapp", "level": "ultra", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "Invalid level" in data["error"]
            assert "ultra" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_error_mentions_valid_levels(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_run",
                    {"product": "myapp", "level": "bad", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "smoke" in data["error"]
            assert "standard" in data["error"]
            assert "thorough" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_uninitialized_project(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_run",
                    {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "not initialized" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_error_for_disallowed_directory(self, tmp_path: Path):
        import ghostqa.mcp.server as srv_module
        from ghostqa.mcp.server import create_server

        original = srv_module._ALLOWED_DIRS
        srv_module._ALLOWED_DIRS = [tmp_path.resolve()]
        mcp = create_server()
        try:
            async def _run():
                result, _ = await mcp.call_tool(
                    "ghostqa_run",
                    {"product": "myapp", "level": "smoke", "directory": "/etc"},
                )
                return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "access denied" in data["error"].lower()
        finally:
            srv_module._ALLOWED_DIRS = original

    def test_error_when_playwright_missing(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)
        try:
            async def _run():
                with patch.dict(sys.modules, {"playwright": None}):
                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "Playwright" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_run_with_mocked_orchestrator_returns_passed_true(self, tmp_path: Path):
        """Full happy-path: orchestrator mocked, no evidence dir, returns fallback result."""
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)

        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = ("# Report\n\nAll passed.", True)

        try:
            async def _run():
                with patch("ghostqa.mcp.server._build_config") as mock_cfg, \
                     patch("ghostqa.mcp.server._check_playwright_available", return_value=None), \
                     patch("ghostqa.engine.orchestrator.GhostQAOrchestrator", return_value=mock_orchestrator):
                    from ghostqa.config import GhostQAConfig
                    cfg = GhostQAConfig()
                    cfg.project_dir = tmp_path / ".ghostqa"
                    cfg.evidence_dir = tmp_path / ".ghostqa" / "evidence"
                    cfg.level = "smoke"
                    mock_cfg.return_value = cfg

                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert "passed" in data
            assert data["passed"] is True
            assert data["run_id"] == "unknown"
        finally:
            _restore_allowed(srv_module, original)

    def test_run_returns_structured_result_when_evidence_exists(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        run_id = "GQA-RUN-20260201-120000-mock"
        run_dir = evidence_dir / run_id
        run_dir.mkdir()
        payload = {
            "run_id": run_id,
            "passed": True,
            "step_reports": [{"step_id": "s1", "passed": True}],
            "findings": [],
            "cost_usd": 0.20,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")

        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = ("# Report", True)

        try:
            async def _run():
                with patch("ghostqa.mcp.server._build_config") as mock_cfg, \
                     patch("ghostqa.mcp.server._check_playwright_available", return_value=None), \
                     patch("ghostqa.engine.orchestrator.GhostQAOrchestrator", return_value=mock_orchestrator):
                    from ghostqa.config import GhostQAConfig
                    cfg = GhostQAConfig()
                    cfg.project_dir = ghostqa_dir
                    cfg.evidence_dir = evidence_dir
                    cfg.level = "smoke"
                    mock_cfg.return_value = cfg

                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert data["passed"] is True
            assert data["run_id"] == run_id
            assert data["summary"]["total_steps"] == 1
            assert data["cost_usd"] == 0.20
        finally:
            _restore_allowed(srv_module, original)

    def test_run_includes_findings_in_result(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        run_id = "GQA-RUN-20260201-130000-find"
        run_dir = evidence_dir / run_id
        run_dir.mkdir()
        payload = {
            "run_id": run_id,
            "passed": False,
            "step_reports": [{"step_id": "s1", "passed": False}],
            "findings": [
                {
                    "severity": "critical",
                    "category": "functional",
                    "description": "Login button not clickable",
                    "step_id": "s1",
                }
            ],
            "cost_usd": 0.50,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")

        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = ("# Report", False)

        try:
            async def _run():
                with patch("ghostqa.mcp.server._build_config") as mock_cfg, \
                     patch("ghostqa.mcp.server._check_playwright_available", return_value=None), \
                     patch("ghostqa.engine.orchestrator.GhostQAOrchestrator", return_value=mock_orchestrator):
                    from ghostqa.config import GhostQAConfig
                    cfg = GhostQAConfig()
                    cfg.project_dir = ghostqa_dir
                    cfg.evidence_dir = evidence_dir
                    cfg.level = "smoke"
                    mock_cfg.return_value = cfg

                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert data["passed"] is False
            assert data["summary"]["findings_count"] == 1
            assert data["findings"][0]["severity"] == "critical"
            assert data["findings"][0]["category"] == "functional"
        finally:
            _restore_allowed(srv_module, original)

    def test_run_error_when_orchestrator_raises(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)

        mock_orchestrator = MagicMock()
        mock_orchestrator.run.side_effect = RuntimeError("Browser crashed")

        try:
            async def _run():
                with patch("ghostqa.mcp.server._build_config") as mock_cfg, \
                     patch("ghostqa.mcp.server._check_playwright_available", return_value=None), \
                     patch("ghostqa.engine.orchestrator.GhostQAOrchestrator", return_value=mock_orchestrator):
                    from ghostqa.config import GhostQAConfig
                    cfg = GhostQAConfig()
                    cfg.project_dir = ghostqa_dir
                    cfg.evidence_dir = ghostqa_dir / "evidence"
                    cfg.level = "smoke"
                    mock_cfg.return_value = cfg

                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert "error" in data
            assert "Run failed" in data["error"]
            assert "Browser crashed" in data["error"]
        finally:
            _restore_allowed(srv_module, original)

    def test_valid_levels_are_accepted(self, tmp_path: Path):
        """All three valid level values should pass level validation (not return Invalid level error)."""
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        _make_initialized_project(tmp_path)

        valid_levels = ["smoke", "standard", "thorough"]
        try:
            for level in valid_levels:
                async def _run(lvl=level):
                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": lvl, "directory": str(tmp_path)},
                    )
                    return _json_result(result)

                data = run_async(_run())
                # The error will be from playwright check or project check — but NOT Invalid level
                if "error" in data:
                    assert "Invalid level" not in data["error"], (
                        f"Level '{level}' was incorrectly rejected"
                    )
        finally:
            _restore_allowed(srv_module, original)

    def test_summary_counts_passed_and_failed_steps(self, tmp_path: Path):
        mcp, srv_module, original = _make_server_with_allowed(tmp_path)
        ghostqa_dir = _make_initialized_project(tmp_path)
        evidence_dir = ghostqa_dir / "evidence"
        run_id = "GQA-RUN-20260201-140000-sum"
        run_dir = evidence_dir / run_id
        run_dir.mkdir()
        payload = {
            "run_id": run_id,
            "passed": False,
            "step_reports": [
                {"step_id": "s1", "passed": True},
                {"step_id": "s2", "passed": True},
                {"step_id": "s3", "passed": False},
            ],
            "findings": [],
            "cost_usd": 0.75,
        }
        (run_dir / "run-result.json").write_text(json.dumps(payload), encoding="utf-8")

        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = ("# Report", False)

        try:
            async def _run():
                with patch("ghostqa.mcp.server._build_config") as mock_cfg, \
                     patch("ghostqa.mcp.server._check_playwright_available", return_value=None), \
                     patch("ghostqa.engine.orchestrator.GhostQAOrchestrator", return_value=mock_orchestrator):
                    from ghostqa.config import GhostQAConfig
                    cfg = GhostQAConfig()
                    cfg.project_dir = ghostqa_dir
                    cfg.evidence_dir = evidence_dir
                    cfg.level = "smoke"
                    mock_cfg.return_value = cfg

                    result, _ = await mcp.call_tool(
                        "ghostqa_run",
                        {"product": "myapp", "level": "smoke", "directory": str(tmp_path)},
                    )
                    return _json_result(result)

            data = run_async(_run())
            assert data["summary"]["total_steps"] == 3
            assert data["summary"]["passed"] == 2
            assert data["summary"]["failed"] == 1
        finally:
            _restore_allowed(srv_module, original)
