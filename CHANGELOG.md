# Changelog

All notable changes to SpecterQA are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.4.0] — 2026-03-01

### Security

- Removed hardcoded token from codebase; updated `.gitignore` to prevent future credential commits
- SSRF protection on orchestrator health checks — outbound requests now validate target URLs against an allowlist before execution
- Security headers added to dashboard viewer (X-Frame-Options, X-Content-Type-Options, CSP)
- Updated `SECURITY.md` with current vulnerability disclosure policy and contact

### Agent Discovery

- Added `llms.txt` at the project root for machine-readable agent discovery following the emerging LLMs.txt standard

### Quality

- Hash-chained cost ledger using SHA-256 — each cost entry links to the previous record, enabling tamper detection
- Config validation on startup: budget values, timeout ranges, severity level, and app_type are now validated with clear error messages before any run begins
- Dependency upper bounds added for all 8 runtime deps (already present for most; now complete and consistent)
- CI coverage integration: `pytest-cov` added to dev extras; Codecov upload step added to CI workflow
- DRY product loading in MCP server — duplicated YAML loading logic consolidated into a shared helper

### Federation

- Shared Mind hook (opt-in, fail-silent): when `SPECTERQA_SHARED_MIND_URL` is set, run results are emitted as observations to the BusinessAtlas Shared Mind substrate; failure to connect is logged and ignored

### Breaking Changes

- **MCP default-deny directory access:** The MCP server's `specterqa_run` tool now rejects all directory paths by default unless `SPECTERQA_ALLOW_ALL_DIRS=1` is set. Previously unset `SPECTERQA_ALLOWED_DIRS` meant permissive access; now unset means deny-all. Set `SPECTERQA_ALLOW_ALL_DIRS=1` to restore the old permissive behavior, or configure `SPECTERQA_ALLOWED_DIRS` with explicit allowed prefixes.
- **Updated model IDs:** Default model references updated from deprecated aliases to `claude-sonnet-4-6` (action decisions) and `claude-opus-4-6` (complex reasoning). Configs specifying old model IDs by alias may see routing changes.

---

## [0.3.0] — 2026-02-23

### Changed

- **Rebrand: GhostQA -> SpecterQA.** Package renamed from `ghostqa` to `specterqa`. All imports, CLI commands, MCP tool names, environment variables, and project directory paths updated accordingly. Key changes:
  - CLI: `ghostqa` -> `specterqa`, `ghostqa-mcp` -> `specterqa-mcp`
  - Python imports: `from ghostqa` -> `from specterqa`
  - Classes: `GhostQAConfig` -> `SpecterQAConfig`, `GhostQAOrchestrator` -> `SpecterQAOrchestrator`
  - MCP tools: `ghostqa_run` -> `specterqa_run`, `ghostqa_list_products` -> `specterqa_list_products`, etc.
  - Env vars: `GHOSTQA_ALLOWED_DIRS` -> `SPECTERQA_ALLOWED_DIRS`, `GHOSTQA_PROJECT_DIR` -> `SPECTERQA_PROJECT_DIR`, etc.
  - Project directory: `.ghostqa/` -> `.specterqa/`
  - Repository URL: `github.com/SyncTek-LLC/ghostqa` -> `github.com/SyncTek-LLC/specterqa`

---

## [0.2.2] — 2026-02-23

### Changed

- Added MCP Registry verification metadata to README (`mcp-name` comment)
- Added `server.json` for Official MCP Registry submission

---

## [0.2.1] — 2026-02-23

### Security

- **[CRITICAL] Command injection fix (GHSA-SPECTERQA-001):** Removed the `check_command` field from the product YAML schema. The field allowed arbitrary shell commands to be embedded in a YAML service definition and executed by the SpecterQA process. Precondition service checks are now limited to TCP connectivity and HTTP health endpoint checks, which are safe. No workaround exists for v0.2.0 and earlier other than removing `check_command` from all product YAMLs. See [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) for full details.
- **MCP directory traversal fix (SPECTERQA_ALLOWED_DIRS):** The MCP server's `specterqa_run` tool now enforces an allowlist for the `directory` parameter when the `SPECTERQA_ALLOWED_DIRS` environment variable is set. Any path not under an allowed prefix is rejected with a structured error response. When the variable is unset, all paths remain accessible (permissive default for local development). Users running the MCP server in shared or multi-user environments should set this variable. See the [README Security section](README.md#security) and [docs/for-agents.md](docs/for-agents.md) for configuration guidance.
- **Credential scrubbing in run artifacts:** Run result JSON files and log output now automatically scrub known credential patterns (API keys, bearer tokens, passwords, connection strings) before writing to disk. Evidence directories are safer to share and commit as CI artifacts.
- **API key template removed from sample config:** The `specterqa init` sample configuration previously included a placeholder `anthropic_api_key: sk-ant-...` in `.specterqa/config.yaml`. This has been replaced with an environment variable reference (`${ANTHROPIC_API_KEY}`) to prevent accidental key commits.

### Added

- **`--plain` mode for CI and screen readers:** `specterqa run --plain` disables Rich formatting, colors, and progress spinners and emits plain text to stdout. Useful in CI environments where Rich markup becomes noise, and for accessibility tools that consume terminal output.
- **`--fail-on-severity` threshold flag:** `specterqa run --fail-on-severity high` causes the run to exit with a non-zero status if any finding at or above the specified severity level is recorded, even if all steps technically passed. Accepted values: `low`, `medium`, `high`, `critical`, `block`. Default behavior (exit 0 on step pass) is unchanged.
- **`specterqa validate` command:** Validates product, persona, and journey YAML files against the SpecterQA schema without running a test. Reports schema errors, missing required fields, and deprecated fields (including `check_command`, which now produces a schema error). Useful in CI as a pre-flight check.
- **`.well-known/agent.json` — A2A Agent Card:** SpecterQA now ships an Agent Card at `.well-known/agent.json` following the [Agent-to-Agent (A2A) protocol](https://google.github.io/A2A/). This allows A2A-compatible agent hosts to discover SpecterQA's capabilities, available tools, input/output schemas, and cost model without prior out-of-band configuration.
- **108 new MCP server tests (248 total):** The MCP server test suite has been expanded from 140 to 248 tests, covering the `SPECTERQA_ALLOWED_DIRS` allowlist enforcement, credential scrubbing behavior, `specterqa validate` tool, the `--plain` and `--fail-on-severity` CLI flags, and edge cases in the `specterqa_run` tool response schema.

### Changed

- The `services.*.check_command` field in product YAMLs is now a schema error. Existing configs containing this field will be rejected by `specterqa validate` and produce a startup error on `specterqa run`. Remove the field and use `health_endpoint` or TCP port checks instead.
- The `specterqa init` sample product YAML no longer includes a `check_command` example.

---

## [0.2.0] — 2026-02-22

Initial public release.

- Persona-based behavioral testing via YAML-configured personas and journeys
- Claude vision model integration (Haiku for simple actions, Sonnet for complex reasoning)
- Playwright-based browser automation with headless support
- macOS native app testing via Accessibility API (pyobjc optional extra)
- iOS Simulator testing via simctl (pyobjc optional extra)
- Budget enforcement with per-run, per-day, and per-month caps
- JUnit XML output for CI integration
- JSON structured output (`--output json`) for programmatic consumption
- Evidence collection: screenshots, findings, cost breakdown, run-result.json per run
- Stuck detection with model escalation (Haiku → Sonnet → abort)
- Template variables in journey steps (`{{persona.credentials.email}}`)
- MCP server (`specterqa-mcp`) exposing `specterqa_run`, `specterqa_list_products`, `specterqa_get_results`, `specterqa_init`
- Tiered model routing with optional local Ollama fallback for zero-cost simple actions
- Federated protocol: swap in custom `AIDecider` or `ActionExecutor` implementations
- 140 unit tests across config, cost tracking, report generation, MCP server, and CLI
