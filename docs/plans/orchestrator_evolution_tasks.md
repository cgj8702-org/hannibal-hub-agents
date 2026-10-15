# Orchestrator Evolution — Task List

**Companion to:** `orchestrator_evolution_plan.md` (same directory). The plan explains *why* and *where*;
this file is the checkable to-do list.

**Status:** Draft for Implementation (2026-10-12)  
**Baseline & Goal:** Transform Hannibal into an asymmetric Multi-Agent Orchestrator, managing CodeRabbit's 10 PRs/hour quota, Jules's owner-only label summoning, and automated review synthesis.

---

## Executive Progress Dashboard

| Phase / Pillar | Completed | Remaining | Active Priority |
| :--- | :---: | :---: | :---: |
| **Phase 1: Zero-Token Pre-Flight Gatekeeper** | 0 | 3 | **P0 (Immediate)** |
| **Phase 2: CodeRabbit Traffic Controller & Quota Guardian** | 0 | 4 | **P1 (High)** |
| **Phase 3: Executive Review Synthesizer** | 0 | 4 | **P2 (High)** |
| **Phase 4: Jules Flight Director & Spec Launchpad** | 0 | 4 | **P3 (Medium)** |
| **Phase 5: Maintainer RBAC & Command Engine** | 0 | 3 | **P4 (Medium)** |
| **Total** | **0** | **18** | — |

---

## Phase 1 — Zero-Token Pre-Flight Gatekeeper (Pillar 1)

*Goal: Intercept broken, docs-only, or off-limits PRs in under 150ms before touching LLMs or external services.*

- [ ] **T1.1 — Off-limits file guardian in pre-flight.**  
  *Seam:* `src/webhook_agent/analysis/diff_filter.py` and `src/webhook_agent/processor.py`.  
  *Action:* Check PR diff filepaths against off-limits list (`.githooks/*`, `.github/workflows/*`, credentials, model name strings, `MODEL_CHAIN.md`). If touched, immediately comment warning and block automated approvals without calling model.  
  *Evidence/Test:* Unit test with fixture touching `.githooks/pre-commit` asserts immediate warning and bypass of agent turn.

- [ ] **T1.2 — Substantive code vs documentation filter.**  
  *Seam:* `src/webhook_agent/analysis/diff_filter.py`.  
  *Action:* Classify PR diff into `DOCS_ONLY` vs `CODE_CHANGE`. For `DOCS_ONLY` diffs (`*.md`, `*.txt`, docstrings only), skip CodeRabbit invocation and LLM audit; run deterministic Markdown format checks only.  
  *Evidence/Test:* Unit test proving docs-only PR does not trigger external review requests.

- [ ] **T1.3 — Aggregate CI gate synchronization for external triggers.**  
  *Seam:* `src/webhook_agent/processor.py::should_process_event` and `src/webhook_agent/review/ci_gate.py`.  
  *Action:* Require aggregate CI evaluation via `evaluate_ci(repo, head_sha)` to return `PASS` for the PR head SHA before dispatching external review tools, rather than relying on a single `check_suite.completed` event alone. Maintain blocking for `PENDING` or `FAIL` aggregate signals.  
  *Evidence/Test:* Unit test proving external review trigger is withheld when one check suite succeeds while other required checks are pending or failing; dispatches only on aggregate `PASS`.


---

## Phase 2 — CodeRabbit Traffic Controller & Quota Guardian (Pillar 2)

*Goal: Protect the strict 10 PRs/hour limit and ensure bot-author compliance.*

- [ ] **T2.1 — Rolling 60-minute quota accountant.**  
  *Seam:* new `src/webhook_agent/state/quota_tracker.py`.  
  *Action:* Implement rolling window invocation counter backed by worker memory with Firestore persistence fallback. Record timestamp for each external review trigger. Provide `can_dispatch_coderabbit() -> tuple[bool, int, float]`.  
  *Evidence/Test:* Time-series unit tests validating 10-call threshold, rolling window expiration, and quota availability calculations.

- [ ] **T2.2 — Proactive throttle shield & queuing.**  
  *Seam:* `src/webhook_agent/github/jules_proxy.py` and `src/webhook_agent/processor.py`.  
  *Action:* When quota is at 8/10 or 9/10, hold CodeRabbit review trigger. Post an informational comment on the PR: *"⚠️ CodeRabbit quota at X/10 this hour. Holding review request for Y minutes to prevent rate limit."*  
  *Evidence/Test:* Unit test confirming quota threshold triggers hold comment and prevents duplicate requests.

- [ ] **T2.3 — Bot persona compliance verification.**  
  *Seam:* `src/webhook_agent/github/bot_identity.py`.  
  *Action:* Assert that PR branch commits are authored by approved machine user (`cgj8702-agents`) rather than `*[bot]`, preventing CodeRabbit from rejecting the PR due to bot-author refusal policies.  
  *Evidence/Test:* Unit test verifying persona check on mock git commits.

- [ ] **T2.4 — Quota status query command (`/quota`).**  
  *Seam:* `src/webhook_agent/processor.py::handle_issue_comment`.  
  *Action:* Allow maintainers to comment `/quota` on any PR/issue to receive instant breakdown of current CodeRabbit and Gemini hourly consumption.  
  *Evidence/Test:* Unit test asserting `/quota` reply format and accurate counts.

---

## Phase 3 — Executive Review Synthesizer (Pillar 3)

*Goal: Replace comment-wall fatigue with a concise 15-line Executive Decision Card.*

- [ ] **T3.1 — Ingest CodeRabbit review webhook events.**  
  *Seam:* `src/webhook_agent/processor.py::route_event` & `should_process_event`.  
  *Action:* Catch `pull_request_review.submitted` where `sender.login == "coderabbitai[bot]"`. Route to `ExecutiveSynthesizer` rather than treating as general review event.  
  *Evidence/Test:* Unit test verifying event routing specifically for CodeRabbit review submissions.

- [ ] **T3.2 — CodeRabbit review comment triage parser.**  
  *Seam:* new `src/webhook_agent/review/coderabbit_synthesizer.py`.  
  *Action:* Parse CodeRabbit's review body and inline comments. Categorize findings into:
  - 🔴 **Actionable Defect:** Security, schema errors, logic breaks, type errors.
  - ⚪ **Cosmetic Suggestion:** Docstrings, formatting, variable naming, styling.  
  *Evidence/Test:* Test with real recorded CodeRabbit review fixtures correctly categorizes findings.

- [ ] **T3.3 — The 15-line Executive Decision Card formatter.**  
  *Seam:* `src/webhook_agent/review/formatter.py`.  
  *Action:* Format concise executive summary:
  - CI & Test Impact (passed count, coverage notes).
  - Repo Invariants check (author, zero-bypass, off-limits).
  - CodeRabbit Triage summary (actionable vs cosmetic counts and links).
  - Bottom-line verdict for Carly ("Safe to merge" vs "Action required on file X").  
  *Evidence/Test:* Formatting test asserting max length, headers, and required sections.

- [ ] **T3.4 — End-to-end synthesizer replay test.**  
  *Seam:* `tests/unit/review/test_executive_synthesizer.py`.  
  *Action:* Replay fixture of CodeRabbit review on replay diffs #258/#260; assert synthesized decision card is generated accurately.  
  *Evidence/Test:* Offline test passes in test suite.

---

## Phase 4 — Jules Flight Director & Spec Launchpad (Pillar 4)

*Goal: Turn owner-only label summoning into an instant 1-click launchpad.*

- [ ] **T4.1 — `/jules-prep` slash command trigger.**  
  *Seam:* `src/webhook_agent/processor.py::handle_issue_comment`.  
  *Action:* Recognize `/jules-prep` on issues or PR threads. Trigger Jules spec generation pipeline.  
  *Evidence/Test:* Unit test confirming `/jules-prep` extracts issue context and initiates generator.

- [ ] **T4.2 — AST-grounded Jules Spec builder.**  
  *Seam:* new `src/webhook_agent/github/jules_spec_builder.py`.  
  *Action:* Use `analysis/ast_analyzer.py` and symbol graph to generate:
  - Target file paths with line references.
  - Architecture constraints and conventions from `AGENTS.md`.
  - Concrete acceptance criteria and verification commands (`./scripts/ruff-all.sh`, pytest).  
  *Evidence/Test:* Unit test generating spec from mock issue; verify all 4 spec sections exist.

- [ ] **T4.3 — 1-click launchpad CTA rendering.**  
  *Seam:* `src/webhook_agent/github/jules_spec_builder.py`.  
  *Action:* Append clear call-to-action block:
  ```markdown
  ---
  🚀 **Jules Mission Ready:** Apply the `jules` label to this issue/PR to summon Jules.
  ```  
  *Evidence/Test:* Formatting test verifying CTA block rendering.

- [ ] **T4.4 — Autonomous delegation failure recovery.**  
  *Seam:* `src/webhook_agent/github/jules_proxy.py`.  
  *Action:* Detect if Jules PR fails CI; automatically analyze failure output and post a follow-up spec comment for Jules to self-correct.  
  *Evidence/Test:* Unit test simulating failing Jules PR and generating targeted correction spec.

---

## Phase 5 — Lightweight Maintainer RBAC & Command Engine (Pillar 5)

*Goal: Safe, authenticated maintainer commands without unauthenticated vulnerability surface.*

- [ ] **T5.1 — Server-side sender permission validator.**  
  *Seam:* new `src/webhook_agent/github/rbac.py`.  
  *Action:* Implement `verify_maintainer(event_payload, gh_repo=None) -> bool`. Enforce an explicit maintainer allowlist (`ALLOWED_MAINTAINERS = {"cgj8702"}`) or verify repo collaborator write/admin permissions directly via the GitHub API, without relying on webhook `author_association` metadata. Refuse unauthorized callers with silent ignore or generic rejection.  
  *Evidence/Test:* Unit tests asserting allowlisted maintainer allowed, unauthorized users rejected even if presenting arbitrary association headers.


- [ ] **T5.2 — Slash command dispatcher.**  
  *Seam:* `src/webhook_agent/processor.py`.  
  *Action:* Centralize routing for `/triage`, `/jules-prep`, `/quota`, `/review`. Ensure all dispatch passes through `verify_maintainer` before execution.  
  *Evidence/Test:* Unit test verifying unauthorized user cannot trigger `/jules-prep` or `/review`.

- [ ] **T5.3 — Audit logging for maintainer actions.**  
  *Seam:* `src/webhook_agent/github/rbac.py`.  
  *Action:* Structured log of actor, command, target PR/issue, and execution result.  
  *Evidence/Test:* Log capture test verifying audit log output.

---

## Immediate Next Actions (Ranked by Leverage)

1. **T1.1 & T1.2:** Implement zero-token pre-flight guardian to immediately protect CodeRabbit quota on docs and off-limits files.
2. **T2.1 & T2.2:** Build rolling 60-minute quota accountant to prevent hitting the 10 PRs/hour ceiling.
3. **T3.1 & T3.2:** Implement CodeRabbit review ingestion and triage classifier.
4. **T4.1 & T4.2:** Build `/jules-prep` AST-grounded spec builder.
