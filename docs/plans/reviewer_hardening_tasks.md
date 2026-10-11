# Reviewer Hardening — Task List

**Companion to:** `reviewer_hardening_plan.md` (same directory). The plan explains *why* and *where*;
this file is the checkable to-do. Update the plan when scope changes, and check items off here.

**Baseline (2026-10-10):** across 55 merged PRs the bot reviewed — 98% blanket-APPROVE, 1-in-55
ever blocked (and that block was a style opinion resolved by adding docstrings), 92% with zero
critical+minor findings. On the byte-identical #241/#258/#260 replay diffs, CodeRabbit found
security and correctness bugs the bot approved. Every task below closes a specific, verified miss.

**How to use:** one task = one PR. Repo rules apply (never commit to `main`, always `uv`, gate with
`./scripts/ruff-all.sh` → targeted `pytest` → full suite before "done"). Details and "evidence it
worked" criteria live in the plan's matching phase.

---

## Priority 0 — Containment (do first; live risk)

- [ ] **T0.1 — De-register `merge_pr` / `update_issue` from `_conversational_agent`.** *(Plan Phase 0.)* Keeps the good Pydantic-gate part of #258, drops the unauthenticated mutation surface. Risk is *registration* + prompt, not the function existing.
- [ ] **T0.2 — Remove their `TOOL_INPUT_SCHEMAS` entries and prompt lines; revert `docs/slash_commands_catalog.md` tool list.** *(Plan Phase 0.)* Leave no instruction telling the model to call them.

## Priority 1 — Security & suppression robustness (Phase 7 + loop avoidance)

- [ ] **T1.1 — Redact the tool-gate failure path.** `core/callbacks.py:290-299` logs full raw `args` and echoes `val_err` to the model. Log tool name + error types/field names only; return a generic error. *(Plan 7.1 — root of CodeRabbit's 🟡 Moderate.)*
- [ ] **T1.2 — Authorize `add_label` server-side.** No sender-permission check; it's the Jules-delegation trigger, so any commenter can summon an autonomous agent. Deny when sender lacks triage/write. *(Plan 7.2.)*
- [ ] **T1.3 — Authorize `create_issue` server-side.** Same comment-driven reachability, same fix. *(Plan 7.2.)*
- [ ] **T1.4 — Make loop-avoidance aware of third-party bots (not just Jules + self).** `should_process_event` (`processor.py:362-379`) suppresses only this bot (`_is_bot_event`) and Jules; any other bot (e.g. `coderabbitai[bot]`) is invisible, so a human/machine reply to a third-party bot's comment wakes the agent → bounded ping-pong. Confirm empirically (post a keyword-free reply on #268 and watch the conversational branch fire, then terminate when the agent's own `hannibal-hub-agents[bot]` reply is suppressed). Fix: generalize suppression to all `*[bot]`/app-originated comments, or add a CodeRabbit-aware allow/deny, so the agent never auto-replies into a third-party bot thread. Add a regression test. *(Discovered live 2026-10-11 on #268; ties to Phase 0 containment.)*

## Priority 2 — Correctness of the input gate (Phase 7)

- [ ] **T2.1 — `extra="forbid"` on `ToolInputBase`.** `tools/schemas.py:18`. Consume alias keys in normalization *before* strict validation so valid aliases survive and typos (`lables`) fail loudly. *(Plan 7.3.)*
- [ ] **T2.2 — Reject blank labels.** `tools/schemas.py:80` / `:101`. `labels=[""]` passes `min_length=1`. Trim per element, reject empty. *(Plan 7.3.)*
- [ ] **T2.3 — Enforce the `recommendation` mandate OR delete the claim.** `review/schemas.py:278` builds `recommendation=""`; the field defaults to `""`. Reconcile with the contradicting `schemas.py:391` guard in the same PR. *(Plan 7.4 + backlog item.)*

## Priority 3 — Grounding wiring defects (Phase 2 Step 2.5; do before flipping the flag)

- [ ] **T3.1 — Track `pr.get_files()` retrieval success separately from `patches`.** Partial iteration currently gets treated as the complete file list → strict mode falsely rejects; empty result triggers a second `get_files()` call. Skip grounding on failed retrieval; only fall back when the gate never attempted it. *(Plan 2.5 #1 — 🟠 Major.)*
- [ ] **T3.2 — Test for *usable patch text*, not mapping non-emptiness.** `if not any(p for p in grounding_patches.values())` so the all-`patch=None` case hits the documented skip. *(Plan 2.5 #2 — 🟡 Minor.)*
- [ ] **T3.3 — Three regression tests.** Partial-iteration failure → skip, not reject; all-`None` patch → skip warning; and a **verdict assertion** that a strict-mode review whose grounding was skipped by a failed `get_files()` never submits `APPROVE` (downgrade to COMMENT or block — encode the choice). *(Plan 2.5.)*

## Priority 4 — Resolution integrity (Phase 8)

- [ ] **T4.1 — Require current-code evidence for RESOLVED.** Build on PR #261's detector: a resolution cites `path` + evidence that must appear in the current diff, else stays UNRESOLVED. *(Plan Phase 8.)*
- [ ] **T4.2 — Route behavioral "Verified Invariants" through the grounding check.** A behavior claim (e.g. "grounding skips gracefully when patch unavailable") needs a test or diff quote; static facts (`X = "0"`) don't. *(Plan Phase 8 — the #260 false-invariant case.)*
- [ ] **T4.3 — Replay #260's bot reviews + a deletion-only fix as harness fixtures.** Assert the inverted resolution flips to UNRESOLVED and the false invariant is rejected; and assert a **deletion-only fix** (offending code removed, no current-code line to quote) still marks RESOLVED — but *via the current-code check confirming the defect is gone*, with the removal hunk/pre-image as deletion evidence only, not as a substitute for that check. *(Plan Phase 8 evidence.)*

## Priority 5 — Structural (Phases 3–6; largest, do once the harness is loud)

- [ ] **T5.1 — Uncertainty outlet + cheap APPROVE→COMMENT downgrade.** `unverified_claims` field; APPROVE without verified invariants falls back to COMMENT and is treated as correct. *(Plan Phase 3 — targets the sycophancy root cause.)*
- [ ] **T5.2 — Stop fabricating citations.** Replace `path="codebase"` invention in `review/schemas.py` normalizers with an explicit unverified bucket; do this *before* re-tightening `VerifiedInvariant.line`. *(Plan Step 2.3.)*
- [ ] **T5.3 — Label the PR body as `AUTHOR CLAIMS (UNVERIFIED)` or remove it from auditor context.** Measure with the 5-gram narrative-echo metric before/after. *(Plan Phase 4.)*
- [ ] **T5.4 — Re-enable ops tools with all six Phase-6 gates in code.** Caller auth, approval+checks on head SHA, base-branch assert, `ALLOW_AUTOMATED_MUTATIONS`, `status.sha` null-guard, audit log. Reconcile the read-only-vs-mutating identity across catalog/SECURITY.md/prompt. *(Plan Phase 6.)*
- [ ] **T5.5 — Prompt pruning, last.** One clause-group per commit, harness before/after numbers in each PR. Never wipe the safety rules. *(Plan Phase 5.)*

## Verify the plan moved the needle

- [ ] **T6.1 — Build the adjudicated label set, then re-score after Phases 3/7/8.** Label a sample of real PRs with independently adjudicated outcomes (human decides which PRs truly had defects). Measure with explicit numerator/denominator: **recall** = adjudicated defects caught ÷ all adjudicated defects (higher better); **missed-finding rate** = 1 − recall (lower better); **false-approve rate** = PRs approved that should have been blocked ÷ all PRs approved (lower better). All must improve vs. baseline. Do **not** use raw approve-rate drop — it is gameable. Keep each metric's name and definition consistent. *(Plan Definition of Done.)*
