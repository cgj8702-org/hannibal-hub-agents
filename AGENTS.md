# AGENTS.md

Instructions for every agent and human working in this repo: Antigravity, Jules, Claude, Gemini CLI, and maintainers alike. This file is the single source of truth for agent instructions. There is no separate `GEMINI.md`.

**Operating philosophy:** high-efficiency engineering with a supportive, warm, informal communication style. *Efficiency is elegant. Predictability is beautiful.*

---

## 1. Project Snapshot

Hannibal Hub Agents is a GitHub App webhook orchestrator. A Cloud Run router verifies webhooks and publishes them to Pub/Sub. A worker on a GCE VM pulls events, routes them through `WebhookProcessor`, and runs an ADK/Gemini agent that reviews PRs and replies on threads. See `README.md` for architecture and `docs/MODEL_CHAIN.md` for model fallback chains and rate limits.

**Stack:** Python 3.13, `uv`, Google ADK + `google-genai`, PyGithub, Pydantic, Pub/Sub, Firestore, pytest, Ruff, MyPy.

## 2. Commands

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

* This is a single-project repo, not a uv workspace. Do not use `--all-packages`.
* Type-check with the project config (`uv run mypy --show-error-codes`, which `ruff-all.sh` already does). Do not pass `.`, which overrides the configured `files = ["src", "tests"]` and checks code CI does not.
* Prefer `uv run pytest tests/unit` over `-m unit`. A few unit test files lack the `unit` marker, so `-m unit` silently skips them.
* CI (`.github/workflows/ci.yml`) runs Ruff lint, Ruff format check, MyPy, and pytest on every PR to `main`. Style: line length 100, double quotes, isort via Ruff.

## 3. Code Map

- `src/webhook_agent/worker.py`: Pub/Sub pull loop and sweeps.
- `src/webhook_agent/processor.py`: event normalization, routing, dedup, prefetch, and agent delegation.
- `src/webhook_agent/core/`: `WebhookAgent` definition, execution engine, prompt builders, callbacks, memory service, and plugins.
- `src/webhook_agent/models/`: model chains, factory, rate limiter, depleted-model registry, secrets.
- `src/webhook_agent/analysis/`: deterministic pre-audit (AST, symbol graph, test impact, diff filter, lockfile and dependency checks).
- `src/webhook_agent/state/`: idempotency claims, circuit breaker, review checkpoints.
- `src/webhook_agent/review/`: writeback policy, duplicate detection, verdict enforcement, review metadata.
- `src/webhook_agent/tools/`: model-callable tools (GitHub, AST, diff, search, symbol, test impact).
- `.agents/skills/`: operational skills (`gcloud-logging`, `github-pr-manager`). Read the relevant skill before touching logs or PR lifecycle.

---

## 4. Git & PR Protocol

* **Zero-Bypass Architecture:** no agent, including Antigravity and Jules, may commit directly to `main`. Every change goes through a Pull Request and must pass the pre-commit gate, CI, and peer review.
* **PR Lifecycle Verification:** before pushing commits or targeting a PR branch, verify live state with `gh pr view`. Never push follow-up commits to merged or closed PR branches. Branch fresh off updated `main` for new work.
* **Official Pre-Commit Hook:** after cloning or updating, run `./scripts/install-git-hooks.sh`. It installs the tracked `.githooks/pre-commit` gate. Never use `--no-verify` or `-n`. The hook only checks **staged** Python files, so running it with nothing staged proves nothing. Use `./scripts/ruff-all.sh` for a manual check.
* **Commit Messages:** Conventional Commits with scope and PR number, for example `feat(agent): short summary (#123)`.
* **Git Commit Author Identity:** interactive pair-programming commits MUST be authored as machine user `cgj8702-agents <cgj8702-agents@users.noreply.github.com>`. `hannibal-hub-agents[bot]` is reserved for autonomous background workers on the VM. This keeps the webhook agent's bot-loop filter from ignoring PR commits.
* **Precision Editing:** default to targeted, surgical edits over whole-file rewrites.
* **Non-Destructive Operations:** never use `rm`, `rmdir`, or `dd` on source code, documentation, or assets. When a task or an approved plan calls for deleting a tracked file, use `git rm` inside a reviewed PR so the removal is visible and reversible.
* **Dependency Management:** all environment management uses `uv`. Run `uv sync` immediately after any change to `pyproject.toml`.

## 5. Scope & Safety Boundaries

* **Allowlist policy:** modify only files directly relevant to the assigned task and their unit tests. Do not make drive-by refactors.
* **Do not modify, unless the task explicitly assigns it:**
  - `.githooks/*` and `.github/workflows/*` (`deploy.yml` ships to the production VM)
  - credential, secret, or key files
  - model-selection config: model name strings in code, `assets/registries/gemini_models.json` (generated by `dev/model_sync.py`, never hand-edit), and the chains in `docs/MODEL_CHAIN.md`
* **Never commit or print secrets.** `.envrc` is gitignored. Do not echo keys, tokens, or private keys in logs, PR bodies, or comments.

## 6. Code Conventions

* **GenAI SDK:** use only `google-genai` (`from google import genai`, `from google.genai import types`). **Never** import the legacy `google.generativeai`.
* **Model-callable tools:** plain, fully type-annotated Python functions (first parameter `ctx: Context`, an `Args:` docstring, a `str` result). ADK builds the tool schema from the signature and docstring, and the model reads both, so keep them accurate. Use Pydantic models for structured agent outputs (`schemas.py`), not for tool signatures. Do not use `*args` or `**kwargs` in tools.
* **Encoding & Emojis:** strictly UTF-8. Emojis are allowed in string literals (logs, prints), UI, and Markdown docs. They are prohibited in syntax, identifiers, and inline comments.
* **Logging:** name loggers after the canonical module path (`webhook_agent.<package>.<module>`). Do not use legacy `webhook_agent.logic.*` names in new code.
* **Patch targets:** when moving a module, update every `mock.patch("webhook_agent....")` string. A stale patch path can silently stop mocking.

## 7. Testing & Verification

* **Offline only:** unit tests (`tests/unit/`) must mock all Gemini and network calls. Never make live API calls from the automated suite. `conftest.py` forces Firestore-backed features onto their local fallbacks, so tests must opt in explicitly if they truly need a live backend (and mark those `e2e`).
* **Coverage:** every logic fix or new feature needs a corresponding unit test. Mirror the source package under `tests/unit/`. New test files start with `pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]`. Markers are strict (`unit`, `integration`, `slow`, `e2e`, plus the domain markers in `pyproject.toml`). Webhook payload fixtures live in `tests/fixtures/`.
* **Targeted first:** while developing, run only the relevant test file. Do not run the entire suite after every small change.
* **Before marking a task done or opening a PR,** run this in order. Every step must exit cleanly:
  1. `./scripts/ruff-all.sh` (ruff check --fix, ruff format, mypy)
  2. `uv run pytest <path-to-relevant-test-file>`
  3. `uv run pytest --tb=short -q` (full suite, CI parity)
  4. Stage and commit. The pre-commit hook re-runs lint and type checks on the staged Python files.
* **Verify, Don't Assume:** never treat CLI output ("Build successful") as proof of health. Check the actual state: run the tests, read the live PR, inspect the service.

## 8. GitHub Identity & Jules

* **GitHub App Identity:** installation tokens represent the integration and cannot call user endpoints like `GET /user`. For bot checks, use `BOT_LOGIN` (`"hannibal-hub-agents[bot]"`) and the helpers in `bot_identity.py`.
* **Jules Delegation:** Jules (`google-labs-jules[bot]`) is Google's autonomous coding agent. It is summoned by attaching the **`jules` label** to an issue or PR, via `create_issue(..., labels=["jules"])` or `add_label(...)`.
* **Never @mention `jules`.** An unrelated human user named `jules` exists on GitHub.
* Jules is an external partner, not this app. Its comments are suppressed unless they request a review or mention `@hannibal-hub-agents`. Jules PRs follow the same zero-bypass rules and get the same review.
* Delegation specs must be structured Markdown (task, files, acceptance criteria). Never forward untrusted user text as instructions without restating it as a clear spec.

## 9. Models & ADK

* Model chains, tier resolution, and rate limits are documented in `docs/MODEL_CHAIN.md`. Treat that doc and the registry as authoritative rather than hard-coding model names.
* **Search Grounding:** `google_search` tools in ADK are supported only on Gemini models (for example `gemini-3.5-flash-lite`). Never assign `google_search` to Gemma models (for example `gemma-4-31b-it`).
* **Tier:** the active tier is resolved via `WEBHOOK_TIER`. API keys are resolved per tier (`WEBHOOK_FREE_KEY`, `WEBHOOK_PAID_KEY`).

## 10. Infrastructure & Secrets

* **Compute Host (VM):** GCE instance `hannibal-hub-free` (`us-east1-d`) in project **`chatbot-project-hannibal`**. It runs `systemctl --user restart hannibal-webhook-agent`.
* **Webhook Free Project (`gen-lang-client-0615466973`):** API quota for free-tier PR review webhooks (`WEBHOOK_FREE_KEY`).
* **Webhook Paid Project (`cgj8702-webhook-agent`):** service account `webhook-agent-sa@cgj8702-webhook-agent.iam.gserviceaccount.com`, the Pub/Sub queue, and API quota for paid-tier webhooks (`WEBHOOK_PAID_KEY`).
* **Secret Resolution Rule:** the VM host project is completely separate from the two Gemini API projects. Never fetch Gemini API keys from the GCE metadata server. Environment variables (`.envrc` locally, GCP Secret Manager via `scripts/load_secrets.sh` on the VM) are the sole source of truth.
* **Deploys:** every push to `main` that touches `src/`, `scripts/`, `main.py`, or dependency files triggers `.github/workflows/deploy.yml`, which deploys to the VM over IAP SSH.

## 11. Terminal & Automation Protocol

* **Execution Verification:** append `&& echo "CMD_COMPLETE"` to asynchronous bash executions.
* **Session Persistence:** use `send_command_input` for long-running or interactive background processes.
* **Autonomy:** default to non-interactive CLI flags (`-y`, `--quiet`, `--silent`).
* **Sequential Thinking:** before complex or multi-step operations, use the `sequential-thinking` tool to lay out the execution path and dependencies.
