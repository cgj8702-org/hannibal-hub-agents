# Reviewer Hardening Plan

**Status:** Re-evaluated & updated (2026-10-12) — active implementation with Zero-Mutation architecture
**Origin:** analysis of PR #258 and its two `hannibal-hub-agents[bot]` reviews, plus the 2026-10-10
55-PR scorecard and the CodeRabbit-vs-bot head-to-head on the #241/#258/#260 replay diffs.
**Goal:** make the PR review bot *unable* to lie about what it verified, instead of *asking* it not to.

**Companion file:** `reviewer_hardening_tasks.md` (same directory) turns every phase below into
checkable tasks with the defect each one closes. Update both when scope changes.

---

## Implementation Status Dashboard (2026-10-12)

Phases 0, 1, 2, 3, 4, 5, and 8 are **completed and verified in CI**.
Phase 6 is **formally retired** due to the adoption of the permanent **Zero-Mutation Architecture**.
Phase 7 has completed redaction (7.1 in PR #308) while schema strictness (7.3/T2.1), recommendation
reconciliation (7.4/T2.3), and third-party bot suppression (7.5/T1.4) remain active priorities.
Phase 6.1 (evaluation on an adjudicated label set) is the final verification milestone.

| Component / Subsystem | Phase | Status | Summary |
| :--- | :--- | :--- | :--- |
| `src/webhook_agent/review/grounding.py` | Phase 2 | Complete | Pure diff grounding verifier using `walk_right_side`. |
| `tests/unit/review/test_grounding.py` | Phase 2 | Complete | 11 unit tests for diff citation acceptance and rejection. |
| `tests/unit/review/test_replay_harness.py` | Phase 1 & 8 | Complete | Offline fixture harness exercising recorded PR replay cases. |
| `src/webhook_agent/tools/github_tools.py` | Phase 0, 2, 6 | Complete | Grounding integration, partial-retrieval handling, ops tools purged. |
| `src/webhook_agent/review/schemas.py` | Phase 2, 3, 7 | Complete / Active | `unverified_claims`, strict invariant serialization; T2.3 active. |
| `src/webhook_agent/tools/schemas.py` | Phase 7 | Active | Pydantic tool gate; `extra="forbid"` (T2.1) active. |
| `src/webhook_agent/core/callbacks.py` | Phase 7.1 | Complete | Tool-gate error paths sanitized and redacted in PR #308. |
| `src/webhook_agent/core/prompts.py` | Phase 4 & 5 | Complete | Author claims labeled unverified; Phase 5 pruning in PR #303. |
| `src/webhook_agent/processor.py` | Phase 7.5 | Active | Third-party bot suppression (T1.4) active. |
| Ops Tools (`merge_pr`, `update_issue`, `add_label`) | Phase 0 & 6 | Retired | Completely removed under Zero-Mutation policy. |


---

## Baseline: measured, not anecdotal (2026-10-10)

The plan originally rested on reading PR #258's reviews. That sample is now quantified across
the bot's **full review history — 55 merged PRs** in `cgj8702-org/hannibal-hub-agents`, parsed
from the `hannibal-review-metadata` footers:

| Metric | Result |
| :--- | :--- |
| PRs where **every** bot verdict was APPROVE | **54 / 55 (98%)** |
| PRs that ever got a REQUEST_CHANGES | **1 / 55** (#242) |
| PRs with zero critical + zero minor findings | **51 / 55 (92%)** |
| PRs with >=1 minor suggestion | 3 |
| PRs with >=1 risk | 23 |

**Read the one block (#242) before trusting the approve rate as caution.** Its single "critical"
was a subjective module-responsibility opinion about a *future* extension, and it was flipped to
APPROVE not by changing code but by **adding docstrings**. A reviewer whose only blocking finding
dissolves under prose is reacting to narrative, not to the diff. The `risks` column (23/55) is the
mechanism: risks never block, so the bot emits them freely and marks them RESOLVED on the next
push regardless of whether the code changed.

**Head-to-head, same code.** The replay PRs in `cgj8702/agents-sandbox` are byte-identical diffs
to #241/#258/#260. CodeRabbit reviewed the replays; the bot reviewed the originals. On #258 alone
CodeRabbit surfaced an unauthenticated `add_label`/`create_issue` path, raw `args`+`val_err`
written to logs, `extra="ignore"` silently dropping typo'd fields, and a `RiskItem.recommendation`
mandate the PR did not actually enforce. The bot approved all four and **praised** the withdrawn
`merge_pr` restore as having "thorough safety gates."

**Caveat to keep honest — this is a hypothesis, not a controlled result.** Not a controlled
trial: CodeRabbit ran paid-Advanced with static analysis and an explicit trigger; the bot is
free-tier, capped at 6 LLM calls behind a rate limiter. My *reading* is that the budget explains
the shallow depth while the near-never-block direction is a design property rather than a
resource ceiling — i.e. more tokens alone would not fix a reviewer structurally unwilling to fail
a PR. I have **no controlled evidence for either half**; this is inference from observational
data (one bot, one codebase, no matched samples). Treat it as a hypothesis to test, not a finding.
To actually separate design from budget, hold the diff constant and vary only the budget/tooling,
then compare — the replay PRs are the natural place to run that.

**Use this as the before-number, but measure accuracy, not approval volume.** A raw drop in
approve-rate is gameable — a bot that blocks everything moves the metric while getting worse. The
honest success measure is precision/recall against **independently adjudicated** outcomes. On a
labeled sample of real PRs, with a human adjudicator deciding which PRs truly had defects:
- **Recall** = adjudicated defects the bot's blocks caught ÷ all adjudicated defects (higher is better).
- **Missed-finding rate** = 1 − recall = adjudicated defects the bot *failed* to catch ÷ all adjudicated defects (lower is better).
- **False-approve rate** = PRs the bot approved that should have been blocked ÷ all PRs the bot approved (lower is better).
Build that labeled set to retrospectively evaluate against the frozen baseline and re-score the completed hardening phases (with remaining phases 7.3–7.5 evaluated upon completion). Do not let a metric's name and its definition drift apart — state numerator and denominator for each.

---

## 0. Read this first (binding instructions for the implementing agent)

### 0.1 Repo rules that override everything (from `AGENTS.md`)

- **Zero-bypass:** never commit to `main`. Every change goes through a PR.
- **Zero-mutation architecture:** Hannibal is strictly a read-only auditor and conversational peer. The model toolset is strictly non-mutating (`read_file`, `review`, `get_issue`). All mutating tools (`merge_pr`, `update_issue`, `add_label`, `create_issue`) are permanently excluded from agent toolchains. Jules delegation is initiated solely by repository owner `cgj8702` attaching labels via GitHub UI.
- **One branch per phase:** `git checkout main && git pull && git checkout -b <phase-branch>`. Before pushing to an existing PR branch, verify live state with `gh pr view <n>`.
- **Always `uv`:** `uv run pytest ...`, never bare `python`/`pip`/`pytest`. Run `uv sync` after any `pyproject.toml` change.
- **Gate before "done":** (1) `./scripts/ruff-all.sh` (2) `uv run pytest <relevant file>` (3) `uv run pytest --tb=short -q`. All must exit clean.
- **Never** `--no-verify`, never force-push, never amend a pushed commit.
- **Off-limits files:** `.githooks/*`, `.github/workflows/*`, credential/secret files, model-name strings, `assets/registries/gemini_models.json`, `docs/MODEL_CHAIN.md`.
- **Commit author:** `cgj8702-agents <cgj8702-agents@users.noreply.github.com>`. Conventional Commits with scope and PR number.
- **Tests are offline.** No live Gemini or GitHub calls in `tests/unit/`. New test files begin with `pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]`.
- Prefer surgical edits over whole-file rewrites.

### 0.2 Teaching mode (owner's explicit request)

For **every** step, in this order:

1. **Before editing:** state the goal, the exact file(s), and the single seam being changed.
2. **Explain why** this seam is the right one, and what the alternative would be.
3. **Make the smallest reversible edit.** Show the diff.
4. **Run the relevant test immediately** and paste the real output (`&& echo CMD_COMPLETE`).
5. **State what would break if this change were wrong**, and how we would notice.
6. **Stop and wait for the owner's confirmation** before the next step.

One step per message. Never batch steps. Never say "should work" — run it.

### 0.3 Anti-patterns the agent must refuse

- Rewriting whole files when a surgical edit works.
- Adding new dependencies (zero new deps is a value in this repo).
- Mocking away the verifier in tests (that defeats the entire point).
- Treating "the output looked better" as evidence. Evidence is a test result or a metric.
- Touching anything in 0.1's off-limits list.
- Starting Phase 2+ before Phase 1's harness exists.

---

## Phase 0 — Containment [COMPLETE]

**Status:** Completed in PR #301 (`75dcaec`). Beyond containment, the ops tools were permanently purged.

**Summary of Resolution:**
- De-registered `merge_pr` and `update_issue` from `_conversational_agent`.
- Removed their entries from `TOOL_INPUT_SCHEMAS` in `tools/schemas.py`.
- Purged all prompt instructions regarding `merge_pr` and `update_issue` from `core/prompts.py`.
- Reverted and aligned `docs/slash_commands_catalog.md` and `SECURITY.md`.
- Permanently deleted `merge_pr` and `update_issue` function definitions from `tools/github_tools.py`.


---

## Phase 1 — Replay harness [COMPLETE]

**Status:** Completed and active in CI suite (`tests/unit/review/test_replay_harness.py`).

**Why:** you cannot improve what you cannot replay. The harness freezes known-bad cases as regression tests.

### Step 1.1 — Record fixtures (network once, then offline forever) [COMPLETE]
Recorded in `tests/fixtures/reviews/pr_258.json`, `pr_241.json`, `pr_260.json`.
Fixtures contain full patches, bot review bodies, and metadata footers. Unit tests run strictly offline.

### Step 1.2 — Write the checkers as pure functions [COMPLETE]
Implemented in `src/webhook_agent/review/grounding.py` and `tests/unit/review/test_replay_harness.py`.
- Non-vacuity guard: checks pass over real fixture content.
- Citation resolvability: verified against diff hunk maps.
- Test-count claims: regex parser verifying test declarations.
- Body/metadata parity: verified invariants round-trip in footer.
- Sycophancy metric: 5-gram containment between PR body and summary.

### Step 1.3 — Live replay runner [COMPLETE]
Implemented in `dev/record_review_fixtures.py` and manual replay utilities.

---

## Phase 2 — Grounding verifier [COMPLETE]

**Status:** Completed and active (`src/webhook_agent/review/grounding.py` and `tools/github_tools.py::review()`).

### Step 2.1 — Pure verifier [COMPLETE]
Implemented `touched_lines_by_file()` and `verify_citations()` in `review/grounding.py`, reusing `diff_tools.walk_right_side()`.

### Step 2.2 — Plug it in, warn-only first [COMPLETE]
Integrated into `review()` in `tools/github_tools.py`, guarded by `STRICT_REVIEW_GROUNDING` (env flag).

### Step 2.3 — Stop fabricating citations [COMPLETE]
Fabrication of `path="codebase"` / `line=None` in `review/schemas.py` was eliminated. Unresolvable invariants are routed to an explicit unverified bucket rather than inventing paths.

### Step 2.4 — Serialize `verified_invariants` into review metadata footer [COMPLETE]
`verified_invariants` are serialized in `review/schemas.py` (`to_markdown`) and parsed in `review/metadata.py` and `review/formatter.py`.

### Step 2.5 — Fix grounding wiring defects [COMPLETE]
Fixed partial-iteration false rejections by tracking retrieval success separately from patches; added usable patch text check (`any(p for p in patches.values())`); enforced that skipped grounding under strict mode downgrades `APPROVE` to `COMMENT`. Verified with unit tests in `tests/unit/review/test_review_tool_validation.py`.

---

## Phase 3 — Give the model an honest exit [COMPLETE]

**Status:** Completed in PR #262.

**Implementation Details:**
1. Added `unverified_claims: list[str]` to `CodeReviewResponse` and `SyncReviewResponse`.
2. Rendered in markdown and serialized into the `hannibal-review-metadata` footer; registered in `formatter.py` detection lists.
3. Cheap downgrade: APPROVE without verified invariants safely falls back to COMMENT without triggering tool retry exhaustion.
4. Validated by comprehensive unit tests.

---

## Phase 4 — Stop feeding the author's narrative in as grounding [COMPLETE]

**Status:** Completed in PR #262.

**Implementation Details:**
- In `core/prompts.py::build_user_message`, PR descriptions injected into auditor context are explicitly labeled `AUTHOR CLAIMS (UNVERIFIED)`.
- Prevents uncritical echoing of author self-assessments in review summaries.

---

## Phase 5 — Prompt pruning [COMPLETE]

**Status:** Completed in PR #303 (`8c56eb5`).

**Implementation Details:**
- Audited `SYSTEM_INSTRUCTION` and `CONVERSATIONAL_INSTRUCTION`.
- Pruned redundant prompt clauses that are strictly enforced in code (echo prohibitions, rigid formatting micromanagement).
- Preserved all essential safety rules (Jules owner-only invocation, Dependabot suppression, secret confidentiality).
- Added regression guards in `tests/unit/core/test_prompts.py`.


---

## Phase 6 — The ops tools [RETIRED & CANCELLED — Zero-Mutation Architecture]

**Status:** Permanently Retired / Cancelled.

**Architectural Rationale:**
The original plan proposed building 6 complex server-side authorization gates (caller auth, approval checks, base-branch assertions, mutation flags, SHA guards, audit logs) to allow re-enabling `merge_pr` and `update_issue`.

The project has firmly committed to a **Zero-Mutation Architecture**:
1. Hannibal is strictly a read-only auditor and conversational companion.
2. All mutating tools (`merge_pr`, `update_issue`, `add_label`, `create_issue`) are permanently excluded from LLM agent toolchains.
3. Jules delegation is initiated exclusively by repository owner `cgj8702` attaching the `jules` label via GitHub's web interface (Google's backend ignores machine-applied labels).
4. Eliminating mutation capabilities at the architectural boundary completely avoids the risk of unauthorized PR merges or issue tampering, making server-side gating of these tools obsolete.

---

## Phase 7 — Input Gate Strictness, Security & Loop Avoidance [ACTIVE]

**Why:** Addresses specific security, correctness, and reliability findings surfaced during the replay diff analysis and live testing.

### 7.1 — Redact the tool-gate failure path [COMPLETE — PR #308]
`core/callbacks.py` was sanitized to log only the tool name and argument keys (`list(args.keys())`), returning a generic validation error to the model without raw argument echoing or leaky `val_err` payload details. Verified in `tests/unit/core/test_callbacks.py`.

### 7.2 — Authorize `add_label` / `create_issue` server-side [RETIRED / OBSOLETE]
Retired due to the Zero-Mutation Architecture. `add_label` was purged from the codebase, and `create_issue` was removed from `_conversational_agent` in PR #308.

### 7.3 — Make the input gate strictly validate fields [ACTIVE — Task T2.1]
In `tools/schemas.py:18`, `ToolInputBase` still specifies `model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)`. This silently drops misspelled or unrecognized parameters.
- Switch `extra="ignore"` to `extra="forbid"`.
- Ensure pre-validation alias coercion (`pr_number`, `issue_number`) consumes aliases before strict validation runs.
*(Note: Blank label validation is retired as label fields were purged.)*

### 7.4 — Enforce or reconcile `RiskItem.recommendation` mandate [ACTIVE — Task T2.3]
In `review/schemas.py:278`, string risks default to `{"risk": r_text, "recommendation": ""}`. Meanwhile line 400 guards promotion with `if not r_item["recommendation"]: continue`. Reconcile schema defaults, normalizers, and prompt text so recommendations are either enforced or placeholder claims are aligned.

### 7.5 — Loop-avoidance for third-party bots and apps [ACTIVE HIGH PRIORITY — Task T1.4]
In `src/webhook_agent/processor.py:362-379`, `should_process_event` suppresses Hannibal's own events (`_is_bot_event`) and Jules comments (without review intent/mention). Third-party bots (such as `coderabbitai[bot]` or GitHub Actions) or human replies within their threads can still trigger conversational agent execution, risking ping-pong loops.
- Generalize suppression to all bot users (`*[bot]`, sender type `Bot`) unless explicitly mentioned with `@hannibal-hub-agents`.
- Add regression tests in `tests/unit/test_processor.py`.

---

## Phase 8 — Resolution integrity (never mark RESOLVED from narrative) [COMPLETE]

**Status:** Completed in PR #262 & PR #296.

**Implementation Details:**
- `review/metadata.py` and `tools/github_tools.py` enforce that a prior finding can only be marked RESOLVED if the reviewer cites current-code evidence verified against the current diff.
- Behavioral invariant claims (such as grounding skip behavior) are routed through diff quote verification.
- Replay cases for inverted resolution rejection and deletion-aware resolution evidence are verified in `tests/unit/review/test_replay_harness.py`.

---

## Cross-cutting: the loop for every single step

1. Write the failing test or harness assertion **first**. Show it failing.
2. Make it pass with the smallest edit.
3. `./scripts/ruff-all.sh` → `uv run pytest <relevant file>` → `uv run pytest --tb=short -q`.
4. Open the PR. The description must contain **evidence**, not adjectives: the command, the output, and the before/after metric if a metric exists.
5. Only then start the next step.

Keep every enforcement behind an env flag defaulting to **off** until its false-positive rate has been measured. Fail-open to observe, fail-closed to enforce.

---

## Definition of Done (Whole Plan)

- [x] Phase 1 harness catches recorded defects and is integrated into CI (`tests/unit/review/test_replay_harness.py`).
- [x] `STRICT_REVIEW_GROUNDING` verified with zero false positives.
- [x] Step 2.5 wiring defects fixed with skipped-grounding COMMENT downgrade.
- [x] Invariant citation fabrication (`path="codebase"`) permanently stopped.
- [x] Uncertainty outlet (`unverified_claims`) implemented and APPROVE→COMMENT downgrade enabled.
- [x] Narrative-echo risk mitigated with `AUTHOR CLAIMS (UNVERIFIED)` labeling.
- [x] Prompt pruned of code-enforced clauses while preserving essential safety rules (PR #303).
- [x] Zero-Mutation Architecture codified: all mutating tools purged or de-registered; `SECURITY.md`, `AGENTS.md`, and catalogs aligned.
- [x] Phase 7.1 tool-gate error path redacted and sanitized (PR #308).
- [ ] Phase 7.5 bot-loop suppression generalized to third-party bots and apps (Task T1.4).
- [ ] Phase 7.3 `extra="forbid"` enforced on `ToolInputBase` (Task T2.1).
- [ ] Phase 7.4 `RiskItem.recommendation` mandate reconciled (Task T2.3).
- [x] Phase 8 resolution integrity enforced with current-code evidence checks (PR #262, #296).
- [ ] Evaluation on independently adjudicated label set (Task T6.1): measure recall, missed-finding rate, and false-approve rate against 2026-10-10 baseline.


## How to tell it went wrong (watch for these)

- Reviews stop posting / repeat submissions: the verifier is too strict. Check the `invalid_findings` reasons in logs and the comment rate limiter.
- Repeated retries in one turn: a reason string is not actionable enough for the model to fix.
- False rejections on binary or oversized files: `pr.get_files()` returns no `patch` for those — that is *unverifiable*, not *wrong*. Decide explicitly which one it is.
- `uv` failures: this checkout may be shallow (no `git blame` history) and the sandbox may block `python-build-standalone` downloads; say so instead of guessing at history.

## Backlog: checkable rules currently living as prose

Each of these is a validator that does not exist yet. Move them out of the prompt as capacity allows:

- "Never say code is 'verified' without citing specific evidence from the diff" → citation verifier (Phase 2).
- "If invariants are insufficient, change verdict to COMMENT" → verifier-gated APPROVE (Phase 3).
- "For every item in `risks_and_edge_cases`, provide a non-empty `recommendation`" → note this one *disarms* the existing guard at `review/schemas.py:391` (`if not r_item["recommendation"]: continue`), whose comment says placeholder fixes "invent action items with no grounding". The mandate and the guard contradict each other; resolve it deliberately.
- "Do NOT flag valid transitive dependency updates as unauthorized scope creep" → already partly enforced by `analysis/dependency_tree.py`; check for duplicates before adding more.

## Glossary

- **Shape validation** — is it well-typed? Pydantic. Free.
- **Provenance validation** — is this claim traceable to evidence that exists? Pure Python over an injected artifact. Free.
- **Policy validation** — am I allowed to do this at all? Needs an API/token check. Costs a round trip.
- **Fail-open / fail-closed** — observe-only versus reject. Every gate starts fail-open and is hardened after measurement.
- **Seam** — the one function where a new check is inserted. If a change needs more than one seam, it is probably two changes.
