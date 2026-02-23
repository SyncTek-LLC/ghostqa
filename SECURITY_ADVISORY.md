# Security Advisory: GhostQA v0.2.0 and Earlier

**Advisory ID:** GHSA-GHOSTQA-001
**Severity:** Critical (P0)
**CVE:** Pending assignment
**Affected versions:** v0.2.0 and all earlier versions
**Fixed in:** v0.2.1
**Published:** 2026-02-23
**Contact:** info@synctek.io

---

## Summary

GhostQA v0.2.0 and earlier contain a command injection vulnerability in the precondition checking subsystem (`_check_preconditions` in `src/ghostqa/engine/orchestrator.py`). An attacker who can supply a malicious product YAML configuration file — either directly or by influencing a connected AI agent via the MCP `directory` parameter — can execute arbitrary commands with the privileges of the process running GhostQA.

This vulnerability is rated **Critical** because it can be triggered remotely through the GhostQA MCP server by any AI agent connected to it, without requiring file system access to the host machine.

---

## Vulnerability Details

### Attack Vector

GhostQA product YAML files support a `check_command` field under service definitions. This field was intended to allow users to verify that backing services (such as a Postgres database) are available before a test run begins.

The value of `check_command` is read from the YAML file and passed to a subprocess execution function without sufficient restriction. Although the implementation uses `shlex.split()` to tokenize the command string (which prevents most shell metacharacter injection), this defense is bypassed by any command that itself spawns a shell, such as `/bin/sh -c '...'`. An attacker can embed arbitrary shell commands inside the string argument passed to `-c`.

### Affected Component

- **File:** `src/ghostqa/engine/orchestrator.py`
- **Function:** `_check_preconditions()` and its helper `_check_command()`
- **Triggered by:** A product YAML file containing a `services` block with a `postgres`-type service and a `check_command` field

### MCP Escalation Path

The GhostQA MCP server (`src/ghostqa/mcp/server.py`) exposes a `ghostqa_run` tool that accepts a `directory` parameter pointing to any filesystem path. Combined with this vulnerability, an attacker who can influence the `directory` parameter of a `ghostqa_run` MCP call — for example, through prompt injection into an application under test, or through direct manipulation of an AI agent — can point GhostQA at a directory containing a malicious product YAML and trigger command execution on the host machine.

This combined attack requires no pre-existing file system access to the host. The MCP server's `directory` parameter has no allowlist restriction in v0.2.0.

### Severity Justification

- **Confidentiality impact:** High — arbitrary command execution allows exfiltration of any file accessible to the process
- **Integrity impact:** High — arbitrary command execution allows modification of any file writable by the process
- **Availability impact:** Medium — commands could terminate the process or consume resources
- **Exploit complexity:** Low — the attack path is straightforward once an attacker controls a YAML file or can influence an MCP `directory` argument
- **No authentication required:** GhostQA's MCP server does not authenticate calling agents in v0.2.0

---

## Affected Products and Versions

| Component | Affected Versions | Status |
|-----------|-------------------|--------|
| `ghostqa` PyPI package | All versions <= 0.2.0 | Vulnerable |
| GhostQA MCP server (`ghostqa-mcp`) | All versions <= 0.2.0 | Vulnerable |
| GhostQA CLI | All versions <= 0.2.0 | Vulnerable (requires YAML access) |

---

## Workarounds

**If you cannot immediately upgrade to v0.2.1:**

1. **Remove `check_command` from all product YAML files.** The `check_command` field is optional. If your product YAMLs do not contain this field, the vulnerable code path is not reached.

   ```yaml
   # Vulnerable configuration (remove the check_command line):
   services:
     postgres:
       check_command: "pg_isready -h localhost"  # REMOVE THIS

   # Safe configuration:
   services:
     postgres:
       url: "postgres://localhost:5432/mydb"
       # health_endpoint or url-based checks are safe
   ```

2. **Do not use the MCP `directory` parameter with untrusted paths.** If you are running the GhostQA MCP server, configure your AI agent to only call `ghostqa_run` against directories you control. Do not allow agent-generated paths to flow unchecked into the `directory` parameter.

3. **Audit product YAML files before running.** If you are using GhostQA in a team environment, review all `.ghostqa/products/*.yaml` files for `check_command` fields before executing test runs.

---

## Fix in v0.2.1

The v0.2.1 release addresses this vulnerability by:

1. **Removing the `check_command` field** from the product YAML schema. Precondition checks will be limited to TCP connectivity checks and HTTP health endpoint checks, which are safe.

2. **Adding `allowed_dirs` enforcement to the MCP server.** The `directory` parameter will be validated against a configurable set of permitted base directories. Any path outside the allowlist will be rejected with a structured error response.

3. **Updating the YAML schema documentation** to remove any reference to `check_command`.

**Upgrade path:**

```bash
pip install --upgrade ghostqa
```

v0.2.1 is available on PyPI now. See the [GitHub releases page](https://github.com/SyncTek-LLC/ghostqa/releases) for release notes.

---

## Disclosure Timeline

| Date | Event |
|------|-------|
| 2026-02-23 | Vulnerability identified by internal security review |
| 2026-02-23 | Advisory drafted and workaround documented |
| 2026-02-23 | Public advisory published (no prior external reports) |
| 2026-02-23 | v0.2.1 released with fixes |

This vulnerability was discovered through internal static analysis of the codebase. No external exploit or abuse in the wild has been observed. The advisory is published proactively to allow current users to apply workarounds before the patch release.

---

## Reporting New Vulnerabilities

Do not open public GitHub issues for security vulnerabilities. Report them to **info@synctek.io**.

See [SECURITY.md](SECURITY.md) for the full security policy, response timelines, and scope definitions.

---

## Credit

Identified by internal security review. No external reporters for this advisory.
