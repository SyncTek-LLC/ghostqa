# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.4.x   | Yes (active)       |
| 0.3.x   | Yes (security fixes only) |
| 0.2.x   | No (EOL)           |
| 0.1.x   | No                 |
| < 0.1   | No                 |

We provide security updates for the latest minor release. Older versions will not receive patches.

## Security Improvements in v0.4.0

- **SSRF protection:** Precondition health-check URLs are now validated before fetching. URLs resolving to private/loopback address ranges (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, ::1) are rejected. Set `SPECTERQA_ALLOW_PRIVATE_URLS=1` to allow private addresses in trusted local development environments.
- **HTTP security headers:** The local dashboard viewer now sets `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer` on all responses.
- **Token hygiene:** The `.mcpregistry_github_token` pattern is now covered by `.gitignore` under the broader `.mcpregistry_*` and `*.token` globs.
- **Agent discovery (`llms.txt`):** A `llms.txt` file is now published at the repo root for agent-readable product metadata. Only usage documentation is included — no source code, internal architecture, or business logic.

## Prior Security Advisories

**GHSA-SPECTERQA-001 (Critical, resolved in v0.2.1):** Command injection vulnerability in `_check_preconditions` affecting v0.2.0 and all earlier versions. An executable allowlist and shell-metacharacter rejection were introduced. Upgrade to v0.2.1 or later.

See [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) for full details on GHSA-SPECTERQA-001.

---

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report vulnerabilities by emailing **info@synctek.io**. Include:

- A description of the vulnerability
- Steps to reproduce or a proof of concept
- The impact or severity as you understand it
- Your name/handle for attribution (optional)

## Response Timeline

| Action                     | Target      |
|----------------------------|-------------|
| Acknowledgment of report   | 48 hours    |
| Initial assessment         | 5 business days |
| Fix or mitigation released | 30 days     |

We will keep you informed of our progress. If the issue is accepted, we will:

1. Develop and test a fix privately
2. Release a patched version
3. Publish a security advisory on GitHub
4. Credit the reporter (unless anonymity is requested)

## Scope

The following are in scope for security reports:

- The `specterqa` Python package (code in `src/`)
- CLI command injection or path traversal
- Unsafe handling of API keys or credentials
- Persona YAML injection leading to unintended behavior

The following are out of scope:

- Vulnerabilities in third-party dependencies (report those upstream)
- Issues requiring physical access to the machine
- Social engineering attacks

## Security Best Practices for Users

- Never commit your `.env` file or API keys to version control
- Use environment variables or a secrets manager for `ANTHROPIC_API_KEY`
- Review persona YAML files from untrusted sources before running them
- Keep SpecterQA and its dependencies updated
