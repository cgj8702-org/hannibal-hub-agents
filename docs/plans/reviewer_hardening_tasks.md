# Reviewer Hardening — Task List

**Companion to:** `reviewer_hardening_plan.md` (same directory). The plan explains *why* and *where*;
this file is the checkable to-do list.

**Status:** Re-evaluated and re-baselined (2026-10-12) following PRs #258–#308 and the adoption of the
**Zero-Mutation Architecture**.

---

## Executive Progress Dashboard (2026-10-12)

| Category | Completed | Retired / Obsolete | Active Remaining |
| :--- | :---: | :---: | :---: |
| **P0: Containment & Ops Purge** | 2 | 0 | 0 |
| **P1: Security & Bot Loop Suppression** | 1 (T1.1) | 2 (T1.2, T1.3) | **1 (T1.4)** |
| **P2: Input Gate & Schema Strictness** | 0 | 1 (T2.2) | **2 (T2.1, T2.3)** |
| **P3: Grounding Wiring Defects** | 3 | 0 | 0 |
| **P4: Resolution Integrity** | 3 | 0 | 0 |
| **P5: Structural & Prompt Pruning** | 4 | 1 (T5.4) | 0 |
| **Evaluation & Verification** | 0 | 0 | **1 (T6.1)** |
| **Total** | **13** | **4** | **4** |

---

## Architectural Pivot: Zero-Mutation Policy

The original hardening plan assumed that mutating operational tools (`merge_pr`, `update_issue`,
`add_label`, `create_issue`) would be retained behind server-side authorization gates (Phase 6 / T5.4)
and sender-permission checks (T1.2, T1.3).

**The project has permanently committed to a Zero-Mutation Architecture:**
1. Hannibal is strictly a read-only auditor and conversational peer.
2. All mutating tools have been permanently purged from the repository (`merge_pr`, `update_issue`, `add_label`), alongside orphaned inspection tools (`get_commit_diff`).
3. `create_issue` has been removed from the conversational agent's toolset (now strictly `[read_file, get_issue]`). The agent provides structured task specifications in thread comments, but repository owner `cgj8702` creates issues and attaches the `jules` label to delegate.
4. Jules delegation is initiated exclusively by the human owner (`cgj8702`) attaching the `jules` label on GitHub (Google's backend ignores agent-created labels).
5. All tasks proposing to authorize, gate, or re-enable mutating tools (**T1.2, T1.3, T2.2, T5.4**) are formally **RETIRED**.

---

## Priority 0 — Containment & Toolchain Purge [COMPLETE]

- [x] **T0.1 — De-register `merge_pr` / `update_issue` from `_conversational_agent`.** *(Plan Phase 0 / PR #301.)* Dropped unauthenticated mutation surface and subsequently purged functions from `tools/github_tools.py`.
- [x] **T0.2 — Remove their `TOOL_INPUT_SCHEMAS` entries and prompt lines; revert `docs/slash_commands_catalog.md` tool list.** *(Plan Phase 0 / PR #301.)* Left zero instructions or schemas for removed ops tools.

---

## Priority 1 — Security & Suppression Robustness (Phase 7 + Loop Avoidance)

- [x] **T1.1 — Redact the tool-gate failure path.** *(Plan 7.1 / PR #308.)* `core/callbacks.py` sanitized: logs only tool name and arg keys (`list(args.keys())`); returns generic validation error without raw argument echoing or leaky `val_err` payload details.
- [x] ~~**T1.2 — Authorize `add_label` server-side.**~~ **[RETIRED / OBSOLETE]** *Reason:* Tool permanently purged from codebase under Zero-Mutation policy. No model or webhook path can execute label mutations.
- [x] ~~**T1.3 — Authorize `create_issue` server-side.**~~ **[RETIRED / OBSOLETE]** *Reason:* `create_issue` removed from `_conversational_agent` in PR #308. Agent possesses zero issue-creation capabilities.
- [ ] **T1.4 — Make loop-avoidance aware of third-party bots (not just Jules + self).** *(Plan 7.5 — ACTIVE HIGH PRIORITY)* `should_process_event` (`processor.py:362-379`) currently suppresses this bot (`_is_bot_event`) and Jules (unless review intent or explicit mention). Other third-party bots and GitHub Apps (e.g., `coderabbitai[bot]`, `github-actions[bot]`) or replies inside their comment threads can still trigger conversational agent execution, creating ping-pong loops. Generalize suppression to all `*[bot]` / app-originated comments (or maintain explicit bot allow/deny rules) so Hannibal never replies into third-party bot threads without explicit user mention. Add regression unit tests in `tests/unit/test_processor.py`.

---

## Priority 2 — Schema Strictness & Input Validation (Phase 7)

- [ ] **T2.1 — `extra="forbid"` on `ToolInputBase`.** *(Plan 7.3 — ACTIVE)* In `tools/schemas.py:18`, `ToolInputBase` still uses `model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)`. Switch to `extra="forbid"` so unrecognized parameters fail fast. Ensure pre-validation field alias normalization consumes legacy aliases (`issue_number`, `pr_number`) before strict validation runs.
- [x] ~~**T2.2 — Reject blank labels.**~~ **[RETIRED / OBSOLETE]** *Reason:* Label tools and label schema parameters were purged from all tool schemas (`CreateIssueInput` no longer has a `labels` field).
- [ ] **T2.3 — Enforce the `RiskItem.recommendation` mandate OR delete the claim.** *(Plan 7.4 — ACTIVE)* In `review/schemas.py:278`, string risks default to `{"risk": r_text, "recommendation": ""}`. Meanwhile line 400 guards promotion with `if not r_item["recommendation"]: continue`. Reconcile the mandate in the schema, normalizers, and prompt text so non-empty recommendations are cleanly handled or the prompt mandate is reconciled with actual behavior.

---

## Priority 3 — Grounding Wiring Defects [COMPLETE]

- [x] **T3.1 — Track `pr.get_files()` retrieval success separately from `patches`.** *(Plan 2.5 #1 / PR #262.)* Handled in `tools/github_tools.py::review()`; skips grounding on failed retrieval rather than falsely rejecting citations.
- [x] **T3.2 — Test for *usable patch text*, not mapping non-emptiness.** *(Plan 2.5 #2 / PR #262.)* Checks `any(patch for patch in grounding_patches.values())` so all-`None` patch diffs correctly trigger skip warnings.
- [x] **T3.3 — Regression tests for retrieval failure and skip verdict.** *(Plan 2.5 / PR #262.)* Tests verify partial retrieval skips without rejecting, all-`None` patches warn, and strict-mode reviews with skipped grounding never submit APPROVE.

---

## Priority 4 — Resolution Integrity (Phase 8) [COMPLETE]

- [x] **T4.1 — Require current-code evidence for RESOLVED.** *(Plan Phase 8 / PR #262, #296.)* Resolution citations must resolve against current diff content; resolutions lacking current-code diff quotes stay UNRESOLVED.
- [x] **T4.2 — Route behavioral "Verified Invariants" through the grounding check.** *(Plan Phase 8 / PR #262, #296.)* Behavioral invariant claims require diff quotes or test evidence.
- [x] **T4.3 — Replay #260's bot reviews + deletion-only fix as harness fixtures.** *(Plan Phase 8 / PR #262, #296.)* Tested in `tests/unit/review/test_replay_harness.py`.

---

## Priority 5 — Structural Hardening & Prompt Pruning [COMPLETE & RE-BASELINED]

- [x] **T5.1 — Uncertainty outlet + cheap APPROVE→COMMENT downgrade.** *(Plan Phase 3 / PR #262.)* `unverified_claims` added to schemas and metadata footer; unverifiable APPROVE downgrades to COMMENT.
- [x] **T5.2 — Stop fabricating citations.** *(Plan Step 2.3 / PR #262.)* Removed `path="codebase"` / `line=None` fabrication; invalid invariants route to unverified bucket.
- [x] **T5.3 — Label the PR body as `AUTHOR CLAIMS (UNVERIFIED)`.** *(Plan Phase 4 / PR #262.)* Context labeled in `core/prompts.py` to prevent narrative echoing.
- [x] ~~**T5.4 — Re-enable ops tools with all six Phase-6 gates in code.**~~ **[RETIRED / CANCELLED]** *Reason:* Permanent commitment to Zero-Mutation Architecture. Ops tools will not be re-enabled.
- [x] **T5.5 — Prompt pruning.** *(Plan Phase 5 / PR #303.)* Pruned redundant code-enforced clauses from `SYSTEM_INSTRUCTION` and conversational prompts while preserving critical safety rules.

---

## Verification & Evaluation Milestone [ACTIVE]

- [ ] **T6.1 — Build the adjudicated label set, then re-score against baseline.** *(Plan Definition of Done — ACTIVE)*
  - Curate a labeled evaluation dataset of merged/replayed PRs with human-adjudicated ground truth.
  - Compute post-hardening metrics:
    - **Recall** = adjudicated defects caught ÷ all adjudicated defects (higher is better).
    - **Missed-finding rate** = 1 − recall (lower is better).
    - **False-approve rate** = PRs approved that should have been blocked ÷ all PRs approved (lower is better).
  - Verify against 2026-10-10 baseline (98% blanket approval, 92% zero findings).

---

## Immediate Next Actions (Ranked by Priority)

1. **T1.4 (Bot Loop Suppression):** Suppress third-party bots (`coderabbitai[bot]`, general `*[bot]`) in `processor.py:should_process_event` to prevent conversational loop triggers.
2. **T2.1 (Input Gate Strictness):** Update `ToolInputBase` in `tools/schemas.py` to `extra="forbid"`, ensuring aliases are coerced before schema validation.
3. **T2.3 (Recommendation Mandate Reconciliation):** Align `RiskItem.recommendation` in `review/schemas.py` with prompts.
4. **T6.1 (Adjudicated Eval Set):** Build evaluation dataset to measure recall and false-approve rate.

