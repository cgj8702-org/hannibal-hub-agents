# 🔒 Security Policy

## Reporting a Vulnerability

We take security seriously. If you discover a security vulnerability in Hannibal Hub Agents, please report it responsibly.

### How to Report
- **Do not** open a public issue for security vulnerabilities
- Email security concerns to the repository maintainers
- Include detailed steps to reproduce the vulnerability
- Include potential impact assessment

### Response Timeline
- Initial response within 48 hours
- Triage and verification within 7 days
- Patch and disclosure coordinated with reporter

## Security Features

This project implements several security measures:

1. **HMAC Signature Verification** - All incoming webhooks are verified at the router level
2. **Short-lived Tokens** - GitHub App installation tokens are cached and rotated hourly
3. **Policy Gates** - `ALLOW_AUTOMATED_MUTATIONS` prevents unexpected automated changes
4. **Least Privilege Tooling** - Agent tool schemas are scoped to specific event types

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| main    | ✅ Active support   |

## Security Best Practices for Contributors

See [AGENTS.md](AGENTS.md) for development security protocols:
- Never commit secrets or credentials
- Use environment variables for sensitive configuration
- Run `./scripts/ruff-all.sh` before submitting PRs
- All changes require PR review (zero-bypass architecture)