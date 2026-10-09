# Reviewer Hardening Plan

**Status:** draft for implementation
**Origin:** analysis of PR #258 and its two `hannibal-hub-agents[bot]` reviews
**Goal:** make the PR review bot *unable* to lie about what it verified, instead of *asking* it not to.

---

## Draft status -- what already exists in this working tree

Phases 1 and 2 are drafted and exercised to the limit of this sandbox. `uv` cannot install
dependencies here, so `uv run pytest` has **not** been run; instead both test modules were
executed with a stdlib harness that stubs `pytest` and shims the package tree, giving
**18 tests: 16 pass, 2 `xfail(strict=True)`, 0 errors**. Run the real thing first.

| Path | State |
| :--- | :--- |
| `src/webhook_agent/review/grounding.py` | Done. Pure, no I/O, reuses `tools/diff_tools.walk_right_side`. |
| `tests/unit/review/test_grounding.py` | Done. 11 unit tests over hand-written diffs, accept and reject paths. |
| `tests/unit/review/test_replay_harness.py` | Done. 7 fixture-driven tests; 2 `xfail(strict=True)` encode live defects. |
| `tests/fixtures/reviews/pr_258.json` | Recorded. 14 changed files, full patches, both bot reviews. |
| `tests/fixtures/reviews/pr_241.json` | Recorded. Reviews + changed paths only (`--patches none`). |
| `dev/record_review_fixtures.py` | Done. Network-only recorder; re-recording is byte-identical. |

**Verify first:** `uv run pytest tests/unit/review/test_grounding.py tests/unit/review/test_replay_harness.py -q`
should report 16 passed, 2 xfailed. Then **start at Phase 2 Step 2.2 (wiring)**, not Step 2.1 --
the verifier is already written. Then Step 2.4, the `snippet` field, then Phases 0/3/4/5/6.

**Two defects this harness found on its first run**, both previously invisible:

1. `verified_invariants` are rendered in the review body but never serialized into the
   `hannibal-review-metadata` footer (Phase 1 Step 1.2 assertion 3, fixed in Step 2.4).
2. Both content extractors in the first draft were silently vacuous (assertion 0).

---

## 0. Read this first (binding instructions for the implementing agent)

### 0.1 Repo rules that override everything (from `AGENTS.md`)

- **Zero-bypass:** never commit to `main`. Every change goes through a PR.
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

## Phase 0 — Containment (do this first, it is short)

**Why:** PR #258 registers `merge_pr` and `update_issue` on `_conversational_agent`, which is triggered by `issue_comment.created` from **any** GitHub user. `merge_pr` (`tools/github_tools.py:531-560`) checks only `pr.draft` and `pr.mergeable is False` — no authorization, no approval check, no CI check. The only mitigation is prose in `core/prompts.py:202-203`. This is a live risk and the PR is still open.

**Zero-risk option (always available):** simply do not merge #258. Nothing else in this plan requires touching it. If the owner wants only containment, stop here.

**Where:** `PR #258`; `core/agent_definition.py` (~225-232, conversational tool list); `tools/github_tools.py:531-560` (`merge_pr`), `:235-274` (`update_issue`); `tools/schemas.py:436-492`; `core/prompts.py:197-203`; `docs/slash_commands_catalog.md:32`; `tests/unit/tools/test_github_ops_tools.py`.

**How (if the owner wants the good part of #258 shipped):** push one scope-reduction commit to the existing PR branch that **removes only the ops-tool surface**, keeping the genuinely good part (the Pydantic input gate in `core/callbacks.py:278-303` + `tools/schemas.py` + its tests):

- unregister `merge_pr` and `update_issue` from `_conversational_agent`;
- remove their entries from `TOOL_INPUT_SCHEMAS`;
- remove the `merge_pr`/`update_issue` prompt lines;
- revert the `docs/slash_commands_catalog.md` tool list to the pre-PR text;
- delete `tests/unit/tools/test_github_ops_tools.py` (or move it to Phase 6's branch).

Leave the function definitions in place if desired — the risk is *registration* plus a prompt that instructs the model to call them, not the existence of the function.

**First command:** `gh pr view 258 --json state,mergeStateStatus,reviewDecision,files` — confirm it is still OPEN before pushing.

**Evidence it worked:** `grep -rn "merge_pr\|update_issue" src/ tests/ docs/` returns no agent registration, no prompt line, no schema entry. Full suite green. Note the bot's approval will be superseded by the push — that is expected; do not chase it.

**Ask your agent:** what does *registering* a tool actually do in ADK, and why is "the function exists in the module" different from "the model can call it"?

**Do not:** merge #258 with `merge_pr` registered, even though CI is green and the bot approved. CI passing is not authorization.

---

## Phase 1 — Replay harness (the foundation; nothing later is provable without it)

**Why:** you cannot improve what you cannot replay. Today there is no way to tell whether a prompt edit or a new validator helped. The harness freezes known-bad cases as regression tests.

**Key rule:** the harness must **fail today** on the known defects. A harness that passes on known-bad input is worthless. Prove it fails first, then leave it failing until Phase 2/3 fix it (or mark those assertions `xfail(strict=True)` so the fix is forced).

### Step 1.1 — Record fixtures (network once, then offline forever)

**Where:** new dir `tests/fixtures/reviews/`.

**How:** use `gh` (outside the offline unit suite) to record, for PR #258 and PR #241:

- `pr.json`: number, title, body, `gh pr diff` patch text per file;
- `reviews.json`: the bot review bodies (from `gh api repos/<owner>/<repo>/pulls/<n>/reviews`) including the `hannibal-review-metadata` footer;
- `expected.json`: the assertions below.

Trim to what is needed and commit the fixtures. Unit tests must read fixtures only.

### Step 1.2 — Write the checkers as pure functions

**Where:** new `src/webhook_agent/review/grounding.py` (shared with Phase 2), plus `tests/unit/review/test_replay_harness.py`.

**Assertions to implement (all deterministic, no model):**

0. **Non-vacuity guard (write this first).** Assert every extractor finds at least one thing in the real fixture. Two extractors in the first draft were silently vacuous — a `^`-anchored regex missing `re.M` (found 0 added tests) and a numeric pattern that could not see an adjective between the number and the noun (`31 robust unit tests` matched nothing). Both produced green tests over a real defect. **A check that matches nothing is worse than no check.**
1. **Citation resolvability (regression guard; passes today).** Every `path:line` citation resolves to a line the diff displays. **Correction to an earlier draft of this plan:** this does *not* catch the recorded wrong-line defect. Review 2 cites `github_tools.py:556` for a claim about the gate at 550-553, but line 556 is `if status.merged:` — a real line inside this diff, so every `path`/`line` check passes it. The durable fix is to require the reviewer to quote the literal source line it cites and compare that quote against `walk_right_side(diff)[1]` (Phase 3). Until then `citation_overlap` is advisory only: measured ~0.2 on the 556 case, and 0.0 on a *legitimate* paraphrased suggestion — which is exactly why it must never gate.
2. **Test-count claims.** Extract `(\d+)[^\d\n]{0,30}?tests?\b` (the adjective-tolerant form) and compare against a recount of added `def test_` / `async def test_` with `re.M`. *Known-bad case:* review 1 says 31; the diff adds 23 (19 in the three files it names).
3. **Body/metadata parity.** Anything rendered in the review body must survive into the `hannibal-review-metadata` footer. *Known-bad case found by the first draft of this harness:* `verified_invariants` are rendered under `### 🛡️ Verified Invariants & Edge Cases` but are **never serialized** — both `to_markdown` metadata blocks in `review/schemas.py` omit the key and `review/metadata.py` never reads it. The evidence that justified an APPROVE is invisible to every downstream consumer, including the resolution tracker.
4. **Prior-decision contradiction.** If an earlier PR's review approved a decision a later review reverses (#241 approves removing `merge_pr` as a Zero-Bypass violation; #258 approves restoring it), the later review must acknowledge the reversal. Heuristic — implement the *detection* as a passing test; track acknowledgement as an advisory in Phase 4, not a hard failure.
5. **Narrative-echo metric.** Compute `ngram_containment(pr_body, review_summary, n=5)` — the fraction of PR-body 5-grams appearing verbatim in the review summary. Record the number; do not fail on it. This is the sycophancy metric used in Phase 4.

**Reuse before you build.** `tools/diff_tools.py` already owns `walk_right_side(diff)` (new-side anchors *and* a `{path: {line: text}}` map), `added_line_anchors`, and `verify_line_reference`; `review/comment_poster.py` already imports them, so the dependency direction is established. **Do not add a second diff parser.**

**Evidence it worked:** `uv run pytest tests/unit/review/test_replay_harness.py -q` shows assertions 2 and 3 failing (as `xfail(strict=True)`) and 0/1/4/5 passing. A draft of this harness already achieves that: 18 tests, 16 pass, 2 `xfail(strict=True)`, 0 problematic. Fixing either defect turns its `xfail` into an XPASS, which the suite then reports as a failure — that is the signal to drop the marker.

**Evidence it worked:** run `uv run pytest tests/unit/review/test_replay_harness.py -q` and paste output showing assertions 1 and 2 fail on the recorded #258 fixtures (use `xfail(strict=True)` or explicit `pytest.fail` markers — state which and why).

**Ask your agent:** why is a *recorded fixture* better than calling GitHub in the test? (Answer to expect: determinism, offline rule, speed, and the ability to test cases that no longer exist upstream.)

**Do not:** put `gh` calls inside `tests/unit/`.

### Step 1.3 — Optional live replay runner (separate, manual)

**Where:** `dev/replay_review.py` (this repo already has a `dev/` dir).

**How:** loads a fixture, runs the current reviewer pipeline against it, writes the produced review to `dev/out/<name>.json` for diffing. This one *does* call the model, so it is manual-only and never imported by tests. Gate it behind an explicit `--live` flag.

**Do not:** wire it into CI.

---

## Phase 2 — Grounding verifier (make lying expensive)

**Why:** the reviewer currently validates only the *shape* of a citation (`tools/github_tools.py:449,453` reject empty/`codebase` paths and non-positive lines). It never checks whether the cited line is real. Extend that existing mechanism from shape to provenance.

**Where:** `src/webhook_agent/review/grounding.py` (new, pure) + one insertion in `src/webhook_agent/tools/github_tools.py::review()`.

### Step 2.1 — Write the pure verifier

**How:** follow the four rules — pure function, injected artifact, structured reasons, never invent.

- `touched_lines_by_file(patches: dict[str, str | None]) -> dict[str, set[int]]` — parse unified-diff hunks (`@@ -a,b +c,d @@`); `+` advances the new-side counter and is a change; ` ` advances but is untouched; `-` does not advance; ignore `\ No newline...`. `patch is None` (binary/too large) yields an empty set = *unverifiable*.
- `verify_citations(findings, touched, diff_text) -> list[str]` — for each finding: path must be in `touched`; `line` must be in the touched set (new-side numbering); `evidence` must appear in the diff text after normalizing whitespace/backticks/case (only enforce for evidence longer than ~24 chars). Return one specific, actionable reason per failure.

**Before writing a hunk parser:** read `src/webhook_agent/tools/` — there is a `verify_line_reference` tool. If it already does hunk math, reuse it instead of adding a second parser. Report what you find before proceeding.

### Step 2.2 — Plug it in, warn-only first

**How:** in `review()`, reuse the existing `invalid_findings` accumulator; build `patches` from the `pr.get_files()` call the function already makes for the investigation gate. Guard enforcement behind `STRICT_REVIEW_GROUNDING` (env, default OFF): log all reasons always; only append to `invalid_findings` when strict.

**Why warn-only:** `MAX_AUDITOR_LLM_CALLS=6` and a 3-comments/minute limiter mean a false-positive verifier can make the reviewer reject itself into exhaustion or never post. Harden only after measuring.

**Evidence it worked:** (1) unit tests for accept *and* reject, asserting the reason text, since the reason is the interface the model reads; (2) the harness from Phase 1 assertion 1 now passes when strict is on; (3) false-positive count on ~5 recent merged PRs is zero (run the verifier over their recorded diffs + reviews).

**Ask your agent:** why must the verifier never call GitHub itself?

### Step 2.3 — Stop fabricating citations

**Why:** `review/schemas.py:488-496` (and the sync twin at ~894-902) *invents* `path="codebase"`, `line=None` for string/evidence-only invariants. `review()` then rejects exactly those values at `:494,498` — so the rescue either kills the review or, on a COMMENT verdict, renders a fake `` (`codebase`) `` citation into a public PR comment. A normalizer must never add information.

**How:** replace fabrication with an explicit *unverified* bucket — drop the item from `verified_invariants` and record it (see Phase 3) instead of manufacturing a path. Same for the risk-promotion default at `review/schemas.py:397` and `:400`.

**Order matters:** do this *before* re-tightening `VerifiedInvariant.line` from `int | None` back to `int` (schemas.py:143-145), otherwise the widened type is what is masking the fabrication.

**Evidence it worked:** a unit test asserting that a string invariant produces no fabricated path, and that `VerifiedInvariant.line` is required again.

**Ask your agent:** what is the difference between *normalizing* and *enriching* a payload, and why is the latter always a bug in a validator?

### Step 2.4 -- Serialize `verified_invariants` into the review metadata footer

**Why:** the harness proved that `verified_invariants` are rendered in the review body under
`### Verified Invariants & Edge Cases` but never written to the `hannibal-review-metadata`
footer. Both `to_markdown` metadata blocks in `review/schemas.py` omit the key, and
`review/metadata.py` never reads it. The evidence that justified an APPROVE is therefore
invisible to every downstream consumer -- including the sync resolution tracker, which is
supposed to track prior findings across all three feedback dimensions.

**Where:** `review/schemas.py` (both metadata blocks), `review/metadata.py` (reader and
`get_actionable_findings`), and the regex key lists in `review/formatter.py` (~30-40).

**How:** add `verified_invariants` to both metadata dicts with the same shape as the other
entries (`path`, `line`, `evidence`, `category: "INVARIANT"`), teach the reader to return
them, and add the key to the formatter's detection list so the text-fallback parser does not
drop it. Then remove the `xfail(strict=True)` marker from
`test_body_and_metadata_agree_about_verified_invariants` -- the suite will fail loudly if you
forget, which is the point of strict xfail.

**Evidence it worked:** that test goes green and the fixture round-trips invariants through
`serialize_review_metadata` / `extract_review_metadata`.

---

## Phase 3 — Give the model an honest exit (remove the incentive to fabricate)

**Why:** `APPROVE` currently *requires* 2-3 invariants with a path and a positive integer line (`core/prompts.py`, and `review()` at `github_tools.py:480-502`). Nothing verifies them, so "insufficient" is self-reported by the model that wants to approve. The schema demands confidence and offers no outlet for uncertainty — that is the structural cause of the sycophancy, not the wording.

**Where:** `src/webhook_agent/review/schemas.py` (schema + `to_markdown` + metadata block), `src/webhook_agent/review/metadata.py` (serialization), `src/webhook_agent/tools/github_tools.py` (APPROVE branch), `src/webhook_agent/core/prompts.py` (JSON key names only), `src/webhook_agent/review/formatter.py` (~30-40, the key-detection regex lists).

**How:**

1. Add `unverified_claims: list[str]` (or `residual_uncertainty`) to `CodeReviewResponse` and `SyncReviewResponse`; render it in markdown and in the `hannibal-review-metadata` footer; add its key to the regex lists in `formatter.py` or the text-fallback parser will silently drop it.
2. Make the *downgrade* cheap: an APPROVE that cannot supply verified invariants must fall back to COMMENT, and the pipeline must treat that as correct behaviour — not a failure to be retried.
3. Enforce it in code, not prose: run Phase 2's verifier over `verified_invariants`; if APPROVE and any invariant fails, return the existing `invalid_findings` error so the model self-corrects within its turn budget.

**Evidence it worked:** unit tests for (a) APPROVE with an unverifiable invariant is rejected with a specific reason; (b) APPROVE with a populated `unverified_claims` and no invented invariants is accepted; (c) metadata round-trips through parse/serialize.

**Ask your agent:** why does a *required* "verified" field pressure a language model into fabrication? What is the cheapest schema change that removes that pressure?

---

## Phase 4 — Stop feeding the author's narrative in as grounding

**Why:** review 1's executive summary tracks the PR description almost sentence-for-sentence (including its false "31 unit tests" figure). The author's self-description is being treated as evidence.

**Where:** locate where PR metadata reaches the auditor — start at `core/prompts.py::build_user_message` and the prefetch path in `processor.py`. Do not assume; find and report the exact injection point first.

**How:** either (a) remove the PR body from the auditor's context entirely, or (b) keep it but label it `AUTHOR CLAIMS (UNVERIFIED)` and require a claim-vs-diff mapping before any claim is repeated in the summary. (b) is usually better for usefulness; (a) is better for rigor. Pick one and justify it in the PR description.

**Evidence it worked:** re-run Phase 1's narrative-echo metric (5-gram containment of PR body in the review summary) before and after. Report both numbers in the PR. This is the only acceptable form of "it feels less sycophantic".

**Ask your agent:** what is the difference between *context* and *evidence*, and why does the prompt currently conflate them?

---

## Phase 5 — Prompt pruning (LAST, never first)

**Why last:** a prompt rewrite is the change least likely to be provable and most likely to feel like progress. Do it only when the harness can measure it.

**Explicitly forbidden:** wiping `SYSTEM_INSTRUCTION` / `CONVERSATIONAL_INSTRUCTION`. They contain real-world safety rules with no test coverage (never @mention `jules` because a human by that name exists; no `@dependabot`; bot-loop suppression; the JSON contract keys; secret handling). Deleting those is not cleanup, it is regression.

**How (mechanical, one commit per clause-group):**

1. Print the instruction strings with line numbers.
2. Classify each clause: **(a) enforced by code** → delete or shorten it (the code is now the source of truth); **(b) a judgment call or tone/role statement** → keep; **(c) checkable but unenforced** → this is a backlog item, not a prompt tweak (see the backlog at the end of this document).
3. Ship one clause-group per commit, each PR description containing the harness before/after numbers.

**Evidence it worked:** harness delta (assertions 1-4 + echo metric) before and after each clause-group, pasted in the PR.

**Do not:** change prompt text and a validator in the same commit. If the numbers move, you will not know which one moved them.

---

## Phase 6 — The ops tools, done properly (separate security track, not part of reviewer quality)

**Why:** Phase 0 blocks the risk; this phase is how you re-enable the capability *safely*, if you still want it. Only start this after Phases 1-3.

**Where:** `tools/github_tools.py` (`merge_pr`, `update_issue`), `tools/schemas.py`, `core/agent_definition.py`, `core/prompts.py`, `docs/slash_commands_catalog.md:32-33`, `SECURITY.md:25`.

**Required gates before `merge_pr` may be registered again — all of them, in code, not prose:**

1. **Caller authorization.** Verify server-side that the acting user is a maintainer/collaborator (e.g. `author_association` from the webhook, or a collaborator-permission lookup). Prompt text saying "when requested by maintainers" is not a control.
2. **Approval state and required checks green** on the head SHA (review decision + check runs).
3. **Base-branch assertion** — refuse if the PR does not target the expected base.
4. **Honour `ALLOW_AUTOMATED_MUTATIONS`** (`review/writeback_policy.py:196`) at the tool level, not only at the event level.
5. **Guard `status.sha` before slicing** (`github_tools.py:557`) — a null SHA after a successful merge currently reports *failure*, inviting a retry against an already-merged PR.
6. **Audit log** every mutating call with actor, tool, target, and result.

**Doc reconciliation in the same PR:** `docs/slash_commands_catalog.md:33` still says "Hannibal is strictly read-only" one line below the tool list this plan changes; `SECURITY.md:25` claims tool schemas are "scoped to specific event types"; the prompt's own "read-only auditor" self-description was removed by PR #258. Decide the real identity and make the three agree.

**Evidence it worked:** unit tests for each gate, each one asserting the *refusal* path as well as the happy path; a test proving a non-maintainer cannot merge.

---

## Cross-cutting: the loop for every single step

1. Write the failing test or harness assertion **first**. Show it failing.
2. Make it pass with the smallest edit.
3. `./scripts/ruff-all.sh` → `uv run pytest <relevant file>` → `uv run pytest --tb=short -q`.
4. Open the PR. The description must contain **evidence**, not adjectives: the command, the output, and the before/after metric if a metric exists.
5. Only then start the next step.

Keep every enforcement behind an env flag defaulting to **off** until its false-positive rate has been measured. Fail-open to observe, fail-closed to enforce.

---

## Definition of done (whole plan)

- Phase 1 harness catches all four recorded defects and is in CI.
- `STRICT_REVIEW_GROUNDING` on, with zero false positives across at least five real PRs.
- No code path can fabricate a `path` or `line` for a claim.
- An uncertainty outlet exists and downgrading APPROVE→COMMENT is cheap and expected.
- Narrative-echo metric measured and materially reduced.
- Prompt contains no checkable-but-unenforced rule, or that rule has a tracked backlog entry.
- `docs/slash_commands_catalog.md`, `SECURITY.md`, and the prompt agree about what the agent is allowed to do.

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
