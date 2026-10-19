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

1. **Webhook Authentication at the Edge** - The deployed Cloud Run router is configured with `WEBHOOK_SECRET` and is intended to verify GitHub's `X-Hub-Signature-256` before publishing events to Pub/Sub. Signature verification happens in a router that is not in this repository and cannot be audited here.
2. **Short-lived Tokens** - GitHub App installation tokens are cached locally (`0600` file permissions in a `0700` cache directory) until shortly before their GitHub-issued `expires_at` time, then refreshed; there is no independent hourly rotation schedule in this repository.
3. **Trigger Authorization** - Webhook events and comment triggers are authorized in `src/webhook_agent/core/trigger_auth.py` by sender association and login identity. Events are processed only if the actor's `author_association` matches `TRIGGER_ALLOWED_ASSOCIATIONS` (default: `OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR`) or if the sender's login matches `TRIGGER_EXEMPT_LOGINS` (default: `cgj8702,cgj8702-agents,dependabot[bot],google-labs-jules[bot]`). Unauthorized triggers are rejected or ignored.
4. **Policy Gates & Mutation Controls** - `ALLOW_AUTOMATED_MUTATIONS` controls automated writeback comments and reviews. It defaults to `1` (enabled) in `src/webhook_agent/constants.py` and `scripts/load_secrets.sh`; when set to `0`, automated GitHub mutations and writebacks are suppressed.
5. **Least Privilege Tooling & Zero-Code-Mutation Policy** - Agent tools are strictly scoped by subagent role:
   - Code Auditor: `read_file`, `search_files`, `list_dir`, and `review`.
   - Conversational Engineer: `read_file`, `search_files`, `list_dir`, and `get_issue`.
   Hannibal cannot mutate code files in the repository or execute arbitrary mutations. Administrative and mutating tool functions (e.g. `merge_pr`, `update_issue`, `add_label`, `create_issue`) are completely excluded from model toolchains.
6. **Diff-Grounded Approvals** - LLM-driven PR reviews are prevented from approving pull requests (`APPROVE`) without diff verification. When patch diffs are unavailable (due to retrieval errors or empty patches), reviews are unconditionally downgraded to `COMMENT`. Strict grounding enforcement is configurable via `STRICT_REVIEW_GROUNDING` (default `0`, where grounding citations are checked and logged; when set to `1`, unverified citations reject the review submission for model correction).
7. **Secret Sanitization** - Model outputs pass through `PromptSanitizerPlugin` (`src/webhook_agent/core/sanitizer_plugin.py`), which redacts GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Google API keys (`AIzaSy`), OpenAI keys (`sk-`), AWS access keys (`AKIA`), PEM private keys, and Authorization headers before they are posted to GitHub.
8. **Subprocess Isolation** - Workspace searches and commands execute in a constrained environment with an allowlist of environment variables (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `PYTHONIOENCODING`) and process-group isolation, preventing leakage of host credentials or process escaping.

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| main    | ✅ Active support   |

## Security Best Practices for Contributors

See [AGENTS.md](AGENTS.md) for development security protocols:
- Never commit secrets or credentials
- Use environment variables for sensitive configuration
- Install and pass the tracked pre-commit hook (`./scripts/install-git-hooks.sh`) before submitting PRs
- All changes require PR review (zero-bypass architecture)