# AGENTS.md

Instructions for every agent and human working in this repo: Antigravity, Jules, Claude, Gemini CLI, and maintainers alike. This file is the single source of truth for agent instructions. There is no separate `GEMINI.md`.

**Operating philosophy:** high-efficiency engineering with a supportive, warm, informal communication style. *Efficiency is elegant. Predictability is beautiful.*

---

## 1. Project Snapshot

Hannibal Hub Agents is a GitHub App webhook orchestrator. A Cloud Run router verifies webhooks and publishes them to Pub/Sub. A worker on a GCE VM pulls events, routes them through `WebhookProcessor`, and runs an ADK/Gemini agent that reviews PRs and replies on threads. See `README.md` for architecture and `docs/MODEL_CHAIN.md` for model fallback chains and rate limits.

**Stack:** Python 3.13, `uv`, Google ADK + `google-genai`, PyGithub, Pydantic, Pub/Sub, Firestore, pytest, Ruff, MyPy.

---

## 2. Commands & Development Gate

Always invoke tools through `uv`. Never run bare `python`, `pip`, or `pytest`.

| Task | Command |
| :--- | :--- |
| Sync environment (after any `pyproject.toml` change) | `uv sync` |
| Install the commit gate (once per clone) | `./scripts/install-git-hooks.sh` |
| Lint, format, and type-check (same as the hook) | `./scripts/ruff-all.sh` |
| One test file | `uv run pytest tests/unit/test_worker.py` |
| One test function | `uv run pytest <file> -k "<test_name>"` |
| All unit tests | `uv run pytest tests/unit` |
| Full suite (what CI runs) | `uv run pytest --tb=short -q` |
| Run the worker locally | `uv run python main.py` |

* Single-project repo, not a uv workspace. Do not use `--all-packages`.
* Type-check using project config (`uv run mypy --show-error-codes`, handled by `ruff-all.sh`). Do not pass `.`, which overrides configured `files = ["src", "tests"]`.
* Prefer `uv run pytest tests/unit` over `-m unit`.
* CI (`.github/workflows/ci.yml`) runs Ruff lint, Ruff format check, MyPy, and pytest on every PR to `main`. Style: line length 100, double quotes, isort via Ruff.

---

## 3. Code Map

- `src/webhook_agent/worker.py`: Pub/Sub pull loop and sweeps.
- `src/webhook_agent/processor.py`: event normalization, routing, dedup, prefetch, and agent delegation.
- `src/webhook_agent/core/`: `WebhookAgent` definition, execution engine, prompt builders, callbacks, memory service, and plugins.
- `src/webhook_agent/github/`: GitHub App authentication, bot identity, and PR context enrichment.
- `src/webhook_agent/models/`: model chains, factory, rate limiter, depleted-model registry, secret resolution.
- `src/webhook_agent/analysis/`: deterministic pre-audit (AST, symbol graph, test impact, diff filter, lockfile and dependency checks).
- `src/webhook_agent/environment/`: local execution environments and ephemeral PR workspace context.
- `src/webhook_agent/state/`: idempotency claims, circuit breaker, review checkpoints.
- `src/webhook_agent/review/`: writeback policy, duplicate detection, verdict enforcement, review metadata, markdown formatting, schemas, and fast-paths.
- `src/webhook_agent/tools/`: model-callable tools (GitHub, AST, diff).
- `.agents/skills/`: operational skills (`gcloud-logging`, `github-pr-manager`). Read the relevant skill before touching logs or PR lifecycle.

---

## 4. Configuration, Tiers & Secrets

Hannibal Hub operates across separate GCP projects for VM compute, secret storage, Pub/Sub, and Gemini quotas. Understanding how config and credentials resolve is critical.

### Secrets vs. Runtime Configuration
* **Secrets (`WEBHOOK_FREE_KEY`, `WEBHOOK_PAID_KEY`, `WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY`, service account keys):**
  - Stored strictly in Google Cloud Secret Manager in the `cgj8702-webhook-agent` project.
  - Sourced into process memory on the VM via `scripts/load_secrets.sh` when `hannibal-webhook-agent.service` starts.
  - **NEVER** store API keys, tokens, or private keys in GCE instance metadata or commit them to git.
* **Non-Sensitive Runtime Attributes (`GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`):**
  - Exposed via `.envrc` locally and GCE VM Instance Metadata in production.
  - Sourced by `scripts/load_secrets.sh` without overriding them from Secret Manager.

### Dynamic Tier Resolution (`free` vs. `paid`)
The worker checks `_resolve_tier()` dynamically per request/sweep using this 4-step cascade:
1. **Explicit Environment Variable:** `WEBHOOK_TIER` (`"free"` or `"paid"`).
2. **GCE VM Instance Metadata:** `http://metadata.google.internal/computeMetadata/v1/instance/attributes/WEBHOOK_TIER` (cached locally for 30s).
   - Changing the `WEBHOOK_TIER` instance attribute in Google Cloud Console or via `gcloud compute instances add-metadata` flips the running agent between free and paid tier keys and quotas within 30 seconds without restarting the service!
3. **Firestore Dynamic Config:** `system_config/runtime -> WEBHOOK_TIER`.
4. **Default:** `"free"`.

When `WEBHOOK_TIER="paid"`, `WEBHOOK_PAID_KEY` and paid RPM/RPD rate limits are used. When `"free"`, `WEBHOOK_FREE_KEY` and free RPM/RPD rate limits apply.

---

## 5. Git & PR Protocol

* **Zero-Bypass Architecture:** No agent or human may commit directly to `main`. Every change goes through a Pull Request and must pass the pre-commit gate, CI, and peer review.
* **PR Lifecycle Verification:** Before pushing commits or targeting a PR branch, verify live state with `gh pr view`. Never push follow-up commits to merged or closed PR branches. Branch fresh off updated `main` for new work.
* **Pre-Commit Hook:** Run `./scripts/install-git-hooks.sh` to install `.githooks/pre-commit`. Never use `--no-verify` or `-n`.
* **Commit Messages:** Conventional Commits with scope and issue/PR reference, e.g. `fix(models): scope depleted model registry (#68)`.
* **Git Commit Author Identity:** Interactive pair-programming commits **MUST** be authored as machine user:
  ```text
  cgj8702-agents <cgj8702-agents@users.noreply.github.com>
  ```
  `hannibal-hub-agents[bot]` is reserved for autonomous background workers on the VM. This prevents the webhook agent's bot-loop filter from ignoring PR commits.
* **Precision Editing & Hygiene:** Targeted, surgical edits over full-file rewrites. Never delete tracked files with raw `rm`; use `git rm` inside a reviewed PR.
* **Dependency Management:** All dependencies use `uv`. Run `uv sync` immediately after any change to `pyproject.toml`.

---

## 6. Code & Architecture Conventions

* **GenAI SDK:** Use only `google-genai` (`from google import genai`, `from google.genai import types`). **Never** import legacy `google.generativeai`.
* **Model-callable Tools:** Plain, fully type-annotated Python functions (`def tool_name(ctx: Context, ...)` with complete `Args:` docstrings and `str` return type). ADK constructs tool schemas directly from function signatures and docstrings. Do not use `*args` or `**kwargs`.
* **Structured Outputs:** Use Pydantic models for structured agent outputs (`schemas.py`), not for tool signatures.
* **Encoding & Emojis:** Strictly UTF-8. Emojis are permitted in strings (logs, prints, UI, markdown), but prohibited in code syntax, variable names, and comments.
* **Logging:** Name loggers after canonical module paths (`webhook_agent.<package>.<module>`).
* **Search Grounding:** Supported only on Gemini models (`gemini-3.5-flash-lite`, etc.). Never assign `google_search` tools to Gemma models.

---

## 7. Testing & Verification

* **Offline-First:** All unit tests (`tests/unit/`) must mock Gemini and network calls. Never make live API calls in automated test suites.
* **Coverage & Markers:** New features or fixes require corresponding unit tests mirroring the package structure. Markers are strict (`unit`, `integration`, `slow`, `e2e`, plus pyproject domain markers).
* **Pre-PR / Pre-Merge Checklist:**
  1. `./scripts/ruff-all.sh` (lint, format, type-check)
  2. `uv run pytest <path-to-relevant-test-file>`
  3. `uv run pytest --tb=short -q` (full test suite, matching CI)
  4. Stage and commit (pre-commit gate runs automatically)
* **Verify, Don't Assume:** Check real outputs and live state; do not take tool success logs as proof of behavioral correctness.

---

## 8. GitHub Identity & Jules

* **GitHub App Identity:** Installation tokens represent the bot identity (`BOT_LOGIN = "hannibal-hub-agents[bot]"`).
* **Jules Delegation:** Jules (`google-labs-jules[bot]`) is summoned by attaching the `jules` label to an issue or PR.
  - Jules only answers the human repository owner (`cgj8702`). Labels added by machine accounts or bots are ignored by Google.
  - Never `@mention` `jules` (an unrelated human user exists on GitHub).
  - The agent creates task issues using the `.github/ISSUE_TEMPLATE/jules_task.yml` template. After the issue is filed, the human owner reviews and attaches the `jules` label to summon Jules.
  - Once Jules responds with a session/task ID (typically via a comment on the issue), the agent interacts with Jules programmatically through the Jules REST API (`GET /v1alpha/sessions/{id}`, `POST /v1alpha/sessions/{id}:approvePlan`, `POST /v1alpha/sessions/{id}:sendMessage`). The agent can approve or deny plans and send messages — it does **not** post progress comments to GitHub threads.
  - Jules PRs undergo the same review and zero-bypass protocol. If Jules authors a PR, `jules_proxy.py` can re-open it under `cgj8702-agents` so CodeRabbit and other review tools can review it.

---

## 9. Infrastructure & Deployment Reference

* **Compute Host (VM):** GCE instance `hannibal-hub-free` (`us-east1-d`) in project `chatbot-project-hannibal`.
  - Service command: `systemctl --user restart hannibal-webhook-agent`
  - Service definition runs: `source scripts/load_secrets.sh && exec uv run python main.py`
* **Free Quota Project (`gen-lang-client-0615466973`):** Free-tier Gemini quota.
* **Paid & Infra Project (`cgj8702-webhook-agent`):** Secret Manager, Pub/Sub queues, and paid Gemini quota.
* **Deploys:** Pushes to `main` touching `src/`, `scripts/`, `main.py`, or dependency files trigger `.github/workflows/deploy.yml` (IAP SSH to VM).
