<!-- Recovered from Cline session 1790290155229_8wvgt; original report preserved verbatim below. -->

## Recovery Provenance

- Source: `/home/carly/.cline/data/sessions/1790290155229_8wvgt/1790290155229_8wvgt.messages.json`
- Recovered message: assistant message 39
- Original report: preserved verbatim; checklist generated from its recommendations.

Analysis complete — I read the tree from its oldest strata forward (creation date + last-touch date per file, oldest first) and cross-checked every elder artifact against current code. Here's the full rot report.

---

# 🩻 Repo Rot / Decay / Drift Report — `hannibal-hub-agents`

**Method:** 139 tracked files aged by first-add commit and last-touch commit (`git log --diff-filter=A` + per-file last-commit), read oldest-first, then verified each stale artifact's claims against current `src/`. Nothing was modified (Plan mode). Verified live via `ruff`, `mypy`, `gcloud projects describe`.

**Repo life phases (from git history):**
- **Jul 1** scaffold → **Jul 2–13** three hard pivots (FastAPI→Pub/Sub, GenAI→ADK, ChromaDB→in-memory) that orphaned several early files
- **Jul 28–Aug 13** ops era (VM/systemd, deploy pipeline, dual-tier handoff, permission audit)
- **Aug 17–28** feature era (proactive evaluator, /fix, /resolve, formatter)
- **Sep 11–14** decommission wave (`feature_agent`, `token_optimized_agent`, `gemma_planner`/`enqueue`/`state_graph` archived) + `logic/` package refactor
- **Sep 23–24** hardening wave (model_factory, idempotency, retry loops)

Most rot is concentrated exactly where you'd predict: everything written **before Sep 11** still describes the world as it was, and the Sep 11–14 purge happened without back-documentation.

---

## 🔴 Tier 1 — Live contradictions (these matter today)

**1. The mandated lint gate is RED right now.** `scripts/ruff-all.sh` (AGENTS.md's required pre-completion check) fails:
```
src/webhook_agent/logic/firestore_registry.py:18: error: Unused "type: ignore" comment  [unused-ignore]
src/webhook_agent/logic/firestore_registry.py:22: error: Incompatible types in assignment  [assignment]
Found 2 errors in 1 file (checked 58 source files)
```
Ruff is clean, mypy is not. Commit `bd80caf` (Sep 13) is literally titled "…achieve zero-error static typing" — so this is silent regression (the file and `uv.lock` changed after, nothing re-ran the gate). Nothing automated notices, because…

**2. AGENTS.md promises CI gating that doesn't exist.** "Every code change MUST… pass automated CI/CD gating" — but `.github/workflows/` contains **only `deploy.yml`** (push-to-main deploy, no tests, no lint, no PR checks). The zero-bypass policy is enforced entirely by discipline.

**3. Model-chain "truth" is 4-way divergent:**

| Source | Says |
|---|---|
| `logic/constants.py` + `rate_limiter.py` (code) | `DEFAULT_WEBHOOK_TIER="free"`; tier resolved **`WEBHOOK_TIER` env → GCE metadata attr → Firestore `system_config/runtime`** |
| `docs/MODEL_CHAIN.md` (Sep 11) | env var **`HANNIBAL_TIER`**; "GEMMA_MODEL defaults to **gemini-3.8-flash**"; cascade 3.8→3.7→3.6→3.5-lite→3.1-lite |
| `src/webhook_agent/MODEL_CHAIN.md` (Aug 3) | Free Tier0 = 3.5-flash-lite… Paid Tier0 = 3.8-flash (different narrative again) |
| `README.md` (Aug 28) | `WEBHOOK_TIER`; "uses gemini-3.5-flash-lite, gemma-4-31b-it, gemma-4-26b-a4b-it" |

Plus `handoff-plan-dual-tiers.md` uses `FREE_KEY`/`PAID_KEY` naming while `load_secrets.sh` exports `WEBHOOK_FREE_KEY`/`WEBHOOK_PAID_KEY`.

**4. Safety-policy default polarity drift.** `ALLOW_AUTOMATED_MUTATIONS` defaults **fail-closed (`"0"`)** in `webhook_agent.py:2356` but **fail-open (`"1"`)** in `tools/auto_fix_feedback.py:71`, `logic/constants.py:29`, and `scripts/load_secrets.sh:58`. Same switch, opposite safe direction depending on which module checks it.

**5. Dead links / deleted-world docs:**
- **`README.md`**: repo-structure tree lists `token_optimized_agent/` (deleted `6e0459d`), `state_graph.py`, `gemma_planner.py`, `enqueue.py` (archived `fbed02a`); mermaid diagram still sells the "5-Node ADK State Graph" as the core engine.
- **`src/webhook_agent/evidence.xml`** (Aug 28, never touched): certifies `class ADKStateGraph` in `state_graph.py` as **"Accurate"** — the file it cites has been archived.
- **`docs/logging_inventory.md`**: 188 rows, **31 point at deleted modules** (`feature_agent/*` decommissioned `f996d38`, plus `enqueue.py`, `state_graph.py`); frozen line numbers.
- **`docs/slash_commands_catalog.md`**: documents `/implement` & `/feature` — plumbing deleted Sep 13 (zero refs left in `src/`).
- **`errors.md`** (Sep 11): snapshot of 17 ruff errors that are now fixed *or* moved into `pyproject.toml`'s ignore list (`190f466` "expand ignore list") — evidence of suppression-over-fix, and fully obsolete.
- **`implementation_plan.md`** (Issue #104): named-logger phases are done, `search_tool.py` exists (correctly on `gemini-3.5-flash-lite`) — plan never closed. Bonus: the file itself says "166+" tests; actual = **236**.
- **`docs/model_router_improvement_plan.md`**: proposes `DispatchingLlm` — never built (zero grep hits); superseded by `RateLimitedGemini` + `callbacks.before_model_callback`. Migration phase 3 *did* happen (no `self._agent.model =` hot-swaps remain).
- **`docs/handoff-plan-dual-tiers.md`**: targets `src/assets/registries/rate_limits.json` (deleted by `feb3b30`, unified into `assets/registries/gemini_models.json`) and `src/logic/rate_limiter.py` (now `src/webhook_agent/logic/`); says "create `tests/unit/logic/` — it does not exist yet" (it does).
- **`cloud_run_function.md`**: the **only** copy of the deployed router's source — `functions_framework`/`github_webhook_router` appear nowhere else in the repo (deployed from Cloud Console per `webhook-router.yaml` annotations). A production edge component exists only as a markdown snippet.

**6. Copy-paste rot:** `.github/dependabot.yml` has a `docker` ecosystem entry pointing at **`/rag_service`** — a directory that doesn't exist here (it's a `hannibal-hub` artifact). Silent no-op forever.

**7. Stale skills actively contradict current law:**
- `github-pr-manager` (Aug 1): instructs template selection from `.github/PULL_REQUEST_TEMPLATE/dev_|prod_pull_request_template.md` — both **deleted Jul 28** (`4eee24f` consolidated to one template); also references `rag_service/`.
- `github-bot-identity` (Jul 28): says PRs **MUST** be authored by `Hannibal-Hub-Agents[bot]`; AGENTS.md now fixes `BOT_LOGIN = "hannibal-hub-agents[bot]"` and reserves bot commits for autonomous VM workers (`cgj8702-agents` for interactive). Its footer says "Last Updated: 2026-07-02" — already false when committed. Both skills hardcode machine path `/home/carly/git-credential-github-app.py` (exists locally, but it's a host-specific path in a repo doc).

**8. Two test trees, no arbiter.** `src/webhook_agent/tests/` (Jul 1 era, **182** test fns, still edited Sep 23) vs `tests/unit/` (Sep 13 era, **54** fns) + `tests/eval/` scaffold. README and the PR template reference only the src-inner tree; AGENTS.md is silent; no `testpaths`; no CI. Conventions will keep forking.

**9. Governance doc duplication.** `AGENTS.md` and `GEMINI.md` are **byte-identical** (4321 b each). Two copies of the constitution = guaranteed future drift.

---

## 🟡 Tier 2 — Frozen era artifacts (age-ordered culprits)

| Artifact | Born | Rot |
|---|---|---|
| `.vscode/bootstrap.sh` | Jul 1–2 | Sets commit email `299140917+cgj8702-agents@…` vs AGENTS.md's `cgj8702-agents@users.noreply.github.com` (gitignored, but it *configures* agents, so the drift leaks into commits) |
| `main.py` | Jul 1 | Sets `logging.basicConfig(level=DEBUG)` globally — the exact hygiene class Issue #104 targeted |
| `scripts/publish_test_message.py` (last touched Jul 31) | Jul 1 | Comment cites payload "produced by `app.py::normalize_payload`" — `app.py` decommissioned **Jul 3** |
| `src/webhook_agent/tests/fixtures/*.json` | Jul 28 | Zero references from any test file |
| `webhook-router.yaml` | Jul 29 | Gitignored console dump of the deployed router; verified namespace `1036232021761` = project number of `cgj8702-webhook-agent` (gcloud), so consistent with AGENTS.md — but a frozen snapshot (`maxScale: 1`, `invoker-iam-disabled: true`) that will silently lie as the service evolves |
| `permission_audit_report.md` | Aug 11 | Historically accurate; items #2/#3 since mitigated; #4 ("Event loop is closed") has **no visible fix artifact** (zero `aclose` hits in `src/`) |
| `hannibal-hub-pr-conversations-last-10.md` | Sep 23 | **81 KB / 1591 lines** of *another repo's* PR conversation dump, tracked, referenced nowhere |
| `logs/` (~950 KB), `.envrc` | — | Ignored, fine (just noting volume) |

Also noted: `docs/review_rules.md` / `review_voice.md` are referenced by no code; the Aug 7 `GITHUB_APP_ON_BEHALF_OF_USER.md` and `gcloud-logging` skill are **still accurate** (good dating discipline there — the gcloud skill's project matrix matches AGENTS.md exactly).

---

## 🟢 What's healthy (not all rot)

- Recent `logic/` package (Sep 23) is coherent: `model_factory`, `rate_limiter` with guarded tier cascade, durable `review_idempotency`, plus tests mirrored in `tests/unit/`.
- `ruff` passes clean; `deploy.yml` verifies `systemctl is-active` rather than trusting output; `constants.py` centralizes project IDs that match reality (verified: free project number `111767043262`, paid `1036232021761`, compute host `1023782330414` = deploy WIF project — all consistent with AGENTS.md topology).
- `skills-lock.json` properly pins the 4 imported `mattpocock` skills; `.python-version` ↔ `requires-python` agree; `pyproject.toml` is genuinely well-tuned.

---

## 🛠️ Proposed remediation plan (for Act mode)

**P0 — make truth enforceable (highest leverage)**
1. Fix the 2 mypy errors in `logic/firestore_registry.py`; re-run `ruff-all.sh` to green.
2. Add `.github/workflows/ci.yml` running `ruff-all.sh` + `uv run pytest` on PRs — makes AGENTS.md's "automated gating" claim real (this alone prevents every future T1-style regression).
3. Unify `ALLOW_AUTOMATED_MUTATIONS` to fail-closed in all four sites (single constant in `logic/constants.py`).
4. Pick one model-chain source of truth: correct `docs/MODEL_CHAIN.md` to code reality (`WEBHOOK_TIER` cascade incl. metadata+Firestore; real defaults), demote `src/webhook_agent/MODEL_CHAIN.md` to a pointer, fix README's model list.

**P1 — purge dead links**
5. Rewrite README structure block + mermaid (drop `token_optimized_agent`/`state_graph`/`gemma_planner`/`enqueue`).
6. Apply repo's own `_archive/` convention (or a "historical" banner) to: `errors.md`, `evidence.xml`, `implementation_plan.md`, `model_router_improvement_plan.md`, `handoff-plan-dual-tiers.md`, `permission_audit_report.md`, `logging_inventory.md`.
7. Update `slash_commands_catalog.md` (drop `/implement`), regenerate or archive `logging_inventory.md`, annotate `cloud_run_function.md` with the durable-idempotency change + note that deployed source isn't in VCS.
8. Remove the `docker /rag_service` entry from `dependabot.yml`; fix the `publish_test_message.py` docstring; decide fixtures' fate (wire or delete).

**P2 — governance hygiene**
9. Refresh both stale skills against AGENTS.md (identity, template phase); `GEMINI.md` → pointer or generated from `AGENTS.md`.
10. Document canonical test layout + update PR template/README commands; consider `testpaths`.
11. Move the 81 KB conversation dump out of git (or into `_archive/`); align `bootstrap.sh` email.

Every fix is doc/config-level and revertible via `git revert`; none require destructive operations. Per AGENTS.md, changes should land as PRs off updated `main`.

**Limitations:** mypy's first run exceeded the tool timeout; the completed background run gave the 2 errors quoted. I couldn't verify `mypy` history on that file (created & untouched since Sep 13), so I can't say whether the regression came from a post-Sep-13 lock bump or the zero-error claim predating the final edit — either way, it's red today.

Want me to start executing? Toggle to **Act mode** and tell me where to begin — my recommendation is **P0.1 + P0.2 together** (fix the gate, then make it impossible to ignore), then the doc purge as a second PR.

---

## Remediation Checklist

**Status:** 17 complete · 9 remaining

The original audit is preserved above. This checklist records verified work and remaining follow-up items.

### Integrity and enforcement

- [x] Make the canonical lint/type-check gate green.
- [x] Document the local-only pre-commit policy.
- [x] Install and test the tracked native hook and clone-local installer.
- [x] Normalize `ALLOW_AUTOMATED_MUTATIONS` to fail closed.
- [x] Re-verify and correct `SECURITY.md`.

### Documentation and dead references

- [x] Remove deleted modules from the README structure and architecture diagram.
- [x] Mark `src/webhook_agent/evidence.xml` as historical.
- [x] Mark historical reports and plans:
  - [x] `errors.md`
  - [x] `implementation_plan.md`
  - [x] `docs/model_router_improvement_plan.md`
  - [x] `docs/handoff-plan-dual-tiers.md`
  - [x] `docs/permission_audit_report.md`
- [ ] Regenerate or archive `docs/logging_inventory.md` and verify its source references.
- [x] Remove retired commands from `docs/slash_commands_catalog.md`.
- [x] Document current Secret Manager and review-idempotency behavior in `cloud_run_function.md`.
- [x] Remove the obsolete `/rag_service` Dependabot entry.
- [x] Correct the stale `publish_test_message.py` comment.
- [ ] Decide whether test fixtures are active or should be removed/archived.

### Governance and repository hygiene

- [ ] Refresh the GitHub identity and PR-management skills.
- [ ] Document the relationship between `AGENTS.md` and `GEMINI.md`.
- [ ] Document the canonical test layout and update test commands.
- [ ] Document the purpose and execution status of `tests/eval/`.
- [ ] Move the large PR-conversation dump to an explicitly historical archive.
- [ ] Align the bootstrap email with the current commit-author policy.
- [ ] Re-run the oldest-file audit after the decommission and refactor waves.

### Verification

- [x] Run the official lint and type-check gate.
- [x] Run focused tests for touched modules.
- [x] Run the full test suite.
- [x] Review documentation claims against the current code.
- [x] Keep remediation changes on an isolated branch for PR submission.
