# Changelog

All notable changes to GhostQA are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.2] — 2026-02-23

### Changed

- Added MCP Registry verification metadata to README (`mcp-name` comment)
- Added `server.json` for Official MCP Registry submission

---

## [0.2.1] — 2026-02-23

### Security

- **[CRITICAL] Command injection fix (GHSA-GHOSTQA-001):** Removed the `check_command` field from the product YAML schema. The field allowed arbitrary shell commands to be embedded in a YAML service definition and executed by the GhostQA process. Precondition service checks are now limited to TCP connectivity and HTTP health endpoint checks, which are safe. No workaround exists for v0.2.0 and earlier other than removing `check_command` from all product YAMLs. See [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) for full details.
- **MCP directory traversal fix (GHOSTQA_ALLOWED_DIRS):** The MCP server's `ghostqa_run` tool now enforces an allowlist for the `directory` parameter when the `GHOSTQA_ALLOWED_DIRS` environment variable is set. Any path not under an allowed prefix is rejected with a structured error response. When the variable is unset, all paths remain accessible (permissive default for local development). Users running the MCP server in shared or multi-user environments should set this variable. See the [README Security section](README.md#security) and [docs/for-agents.md](docs/for-agents.md) for configuration guidance.
- **Credential scrubbing in run artifacts:** Run result JSON files and log output now automatically scrub known credential patterns (API keys, bearer tokens, passwords, connection strings) before writing to disk. Evidence directories are safer to share and commit as CI artifacts.
- **API key template removed from sample config:** The `ghostqa init` sample configuration previously included a placeholder `anthropic_api_key: sk-ant-...` in `.ghostqa/config.yaml`. This has been replaced with an environment variable reference (`${ANTHROPIC_API_KEY}`) to prevent accidental key commits.

### Added

- **`--plain` mode for CI and screen readers:** `ghostqa run --plain` disables Rich formatting, colors, and progress spinners and emits plain text to stdout. Useful in CI environments where Rich markup becomes noise, and for accessibility tools that consume terminal output.
- **`--fail-on-severity` threshold flag:** `ghostqa run --fail-on-severity high` causes the run to exit with a non-zero status if any finding at or above the specified severity level is recorded, even if all steps technically passed. Accepted values: `low`, `medium`, `high`, `critical`, `block`. Default behavior (exit 0 on step pass) is unchanged.
- **`ghostqa validate` command:** Validates product, persona, and journey YAML files against the GhostQA schema without running a test. Reports schema errors, missing required fields, and deprecated fields (including `check_command`, which now produces a schema error). Useful in CI as a pre-flight check.
- **`.well-known/agent.json` — A2A Agent Card:** GhostQA now ships an Agent Card at `.well-known/agent.json` following the [Agent-to-Agent (A2A) protocol](https://google.github.io/A2A/). This allows A2A-compatible agent hosts to discover GhostQA's capabilities, available tools, input/output schemas, and cost model without prior out-of-band configuration.
- **108 new MCP server tests (248 total):** The MCP server test suite has been expanded from 140 to 248 tests, covering the `GHOSTQA_ALLOWED_DIRS` allowlist enforcement, credential scrubbing behavior, `ghostqa validate` tool, the `--plain` and `--fail-on-severity` CLI flags, and edge cases in the `ghostqa_run` tool response schema.

### Changed

- The `services.*.check_command` field in product YAMLs is now a schema error. Existing configs containing this field will be rejected by `ghostqa validate` and produce a startup error on `ghostqa run`. Remove the field and use `health_endpoint` or TCP port checks instead.
- The `ghostqa init` sample product YAML no longer includes a `check_command` example.

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
- MCP server (`ghostqa-mcp`) exposing `ghostqa_run`, `ghostqa_list_products`, `ghostqa_get_results`, `ghostqa_init`
- Tiered model routing with optional local Ollama fallback for zero-cost simple actions
- Federated protocol: swap in custom `AIDecider` or `ActionExecutor` implementations
- 140 unit tests across config, cost tracking, report generation, MCP server, and CLI
