# Security Policy

## Active Security Advisory

**GHSA-GHOSTQA-001 (Critical):** Command injection vulnerability in `_check_preconditions` affecting v0.2.0 and all earlier versions.

**Workaround (immediate):** Remove `check_command` from all product YAML files.
**Fix:** Upgrade to v0.2.1 when released.

See [SECURITY_ADVISORY.md](SECURITY_ADVISORY.md) for full details.

---

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | Yes (active)       |
| 0.1.x   | No                 |
| < 0.1   | No                 |

We provide security updates for the latest minor release. Older versions will not receive patches.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report vulnerabilities by emailing **security@synctek.dev**. Include:

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

- The `ghostqa` Python package (code in `src/`)
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
- Keep GhostQA and its dependencies updated
