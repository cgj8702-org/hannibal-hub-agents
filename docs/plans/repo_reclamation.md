# Repo Reclamation: Working Notes & Action Plan

**Status:** draft, living document
**Written on:** branch `cline/mkqr8t4d`
**Purpose:** one place where the diagnosis, the decisions, and the follow-through live — so that delegating execution to an agent never again means losing the thread of intent.

> **Note:** Part 1 (personal working notes) is intentionally kept out of version control. Parts 2–7 are self-contained — nothing below depends on Part 1.

---

## How to use this document

- **Part 2** is the diagnosis in one page.
- **Part 3** is the evidence — every number here was measured, not estimated.
- **Part 4** is the work, sequenced. Every item has a stable ID (`B1`, `C2`, …) so you can refer to them in commits and PRs.
- **Part 5** is the set of decisions only you can make, each ID'd (`P1`…`P7`). Nothing in Part 4 that depends on a `P`-decision should start before that decision is recorded.
- **Parts 6–7** are loose ends and the guardrails that stop this recurring.
- **Part 8** is the event-surface and observability findings from live gcloud logs, plus plans `EV-A`…`EV-F`.

---

# Part 2 — The diagnosis in one page

## 2.1 You don't have a direction problem. You have a missing layer.

Every capability question — *"should it file issues?", "should it resolve threads?", "should it be allowed to talk about code?"* — had to be expressed somewhere. The only available places were **the tool list** and **the agent count**. So that's where every policy decision went.

That's the oscillation, explained:

- A tool is either registered or not — binary, and global to the whole agent, for every event.
- So *"it may resolve a thread when discussing a review comment, but never while triaging an issue"* can only be expressed by **splitting the agent** (topology as policy) or by **writing instructions the model may ignore** (prompt as policy).

You alternated between those two for months. Structure has been doing policy's job, and structure is a bad policy engine: expensive to change, and binary when you need it conditional.

## 2.2 The fossils prove it

`SECURITY.md` §5 says `create_issue` is "completely excluded from model toolchains." `AGENTS.md` §8 says "have API-created issues follow the same fields" — as if the bot creates them. Those sentences describe two different eras of the same flip. The docs contradict each other because **the policy was never written down anywhere**; it was whatever the tool list happened to say that month.

## 2.3 The fix is not "pick the right structure"

It's to give policy its own home, so that structure stops carrying the weight. Then:

- "1 agent or 2?" becomes a mechanical question with no security implications.
- Every capability question becomes a one-line change in one file.
- The docs stop drifting, because they can be generated from the policy.

**The acceptance test:** if someone asks *"how do I change whether the bot may resolve a review thread?"*, can you point at **one file** and change a line? Today the answer is no — it's spread across a tool list, two agent definitions, seven call sites, and a prompt.

## 2.4 It's a churn problem, not a complexity problem

738 commits, but only 19,532 lines of source. High churn, small codebase. That's the signature of many small reversals, not an unmaintainable monster. Those two problems have very different fixes, and yours is the easier one.

---

# Part 3 — The evidence base

Everything here was measured in-session. Reusable as justification in PR bodies.

## 3.1 Repository shape

| Metric | Value |
|:--|:--|
| Commits (Jul 1 → Oct 17, 108 days) | 738 (~7/day) |
| Source lines | 19,532 |
| Test lines | 19,193 (**1:1 ratio — normal**) |
| Test functions | 819 |
| Test files | 61 |
| Exact structural test clones | **0** |
| Near-duplicate test families (>0.90 similarity) | 74, containing 195 tests |
| Collapsible to parametrized form | 819 → ~698 (saves ~15%) |
| `parametrize` usages | 5 |
| Regression-marked (skipped by default) | 9 |

## 3.2 Where the mass actually is

| Package | Lines | |
|:--|--:|:--|
| `review/` | 6,723 | **a third of everything** |
| `core/` | 3,057 | the only part that thinks |
| `analysis/` | 2,761 | deterministic, no LLM |
| `models/` | 1,566 | |
| `tools/` | 1,315 | |
| `github/` | 1,295 | |
| `state/` | 834 | |
| `environment/` | 588 | |
| `processor.py` | 914 | the spine |
| `worker.py` | 406 | |

**The thinking core is ~1,800 lines**: `agent_definition.py` (445) + `execution.py` (759) + `prompts.py` (583). That's the part you need to understand cold. Not 19,500.

## 3.3 Churn hotspots (commits touching each file)

| Commits | File |
|--:|:--|
| 98 | `processor.py` ← by far the hottest |
| 42 | `agent_definition.py` |
| 27 | `tools/github_tools.py` |
| 23 | `core/prompts.py` |
| 16 | `review/schemas.py` |
| 15 | `core/execution.py` |
| 9 | `review/grounding.py` |

## 3.4 The seven places that encode "is this a review event?"

This is the duplication the policy layer collapses:

1. `review/writeback_policy.py:70` — `_is_formal_review_eligible` (5 events)
2. `github/pr_context.py:32` — `_should_prefetch_diff` (5 events)
3. `review/ci_gate.py:28` — `GATED_EVENTS` (4 events)
4. `models/model_chain.py:175`
5. `core/execution.py:296`
6. `core/prompts.py:269`
7. `processor.py:297` + `processor.py:338` — two near-identical mapping dicts

## 3.5 Proof the system works

PR #375 (`canary/test-cross-file-contract-trap`), 2026-10-17:

| Time (UTC) | Event |
|:--|:--|
| 14:24:44 | PR opened |
| 14:25:58 | Bot posts **REQUEST_CHANGES** — 74 seconds later |
| 15:38:10 | Bot posts **APPROVE** with a Resolution Tracker |
| 15:38:11 | Prior review dismissed |
| 16:28:13 | Merged |

The finding was **correct**: `analyze_diff_hunks(filtered_text)` passed diff text as the `file_path` parameter while leaving `diff_text` at its default `""`, silently disabling AST risk scoring. The bot named the right fix and then verified it on re-review, citing `diff_filter.py` lines 274-281.

One flaw worth noting: the critical issue had `"path": ""`, rendered as `_(no file cited)_` — the bot found a cross-file contract bug but couldn't attach it to a file, because the bug is an *absence* in a different file from the one changed. That's exactly what `review/grounding.py` (989 lines) exists to reason about, and it's where the remaining real complexity lives.

---

# Part 4 — Actionable steps

Sequenced. **Do not skip Phase 0** — it's what stops the churn from resuming mid-cleanup.

Every item is a discrete PR-sized unit. Items marked 🧊 are pure deletion and behavior-neutral (easiest to review, highest morale payoff, do these first).

---

## Phase 0 — Stop the bleeding (process, no code)

### A1. Freeze the blast radius
No new tools, modes, agents, or runners until Phase 4 lands. This is where the damage came from; a cleanup that starts with new features will not finish.

### A2. Kill the blueprint's re-additions
The `Unified Autonomous Pair Programming Agent` plan proposes re-adding `create_issue` (round six) and handing `resolve_review_thread` to the model. Both are reversals of deliberate decisions. **Status: rejected.** Do not implement.

### A3. Decide the mutation policy, in writing, once
`AGENTS.md` §8 ("have API-created issues follow the same fields") and `SECURITY.md` §5 ("`create_issue` … completely excluded from model toolchains") contradict each other. This is a doc bug *and* a policy gap. Answer it explicitly before any tool work:

- May the bot create issues? → if yes, add the tool **behind the Phase 4 gate** with no `labels` parameter at all. If no, delete the AGENTS.md sentence.
- Same question for: labels, review threads, commits, merges.

Record the answers in `SECURITY.md` only. One place.

### A4. Adopt the one-file rule
Any future capability question must be answerable by editing **one file**. If it isn't, that's a signal to add the capability to the policy layer first, not to change the topology.

---

## Phase 1 — Behavior-neutral deletions 🧊

All verified by hand with zero reachable callers. One PR, or two if you prefer smaller diffs.

### B1. Delete `normalize_event_type()` 🧊
- **File:** `src/webhook_agent/processor.py:297` — 26 lines
- **Why:** zero references anywhere (verified by grep — only its own definition line). `route_event` (`:327`) is the live implementation, called at `:473` and `:599`. This is not "duplication to consolidate," it is dead code.
- **Note:** this also removes the confusing near-identical mapping dict at `:301-316`. The dict at `:338-355` stays.

### B2. Delete `_create_fallback_agent()` 🧊
- **File:** `src/webhook_agent/core/agent_definition.py:381` — 10 lines
- **Why:** zero references anywhere. (It only rebuilds `_runner`, not `_conversational_runner` — moot, since nothing calls it.)

### B3. Delete `defang_content()` 🧊
- **File:** `src/webhook_agent/review/duplicate_detector.py:107` — 3 lines
- **Why:** zero references anywhere.

### B4. Delete the orphaned canary module 🧊
- **Files:** `src/webhook_agent/analysis/audit_canary.py` (40 lines) + `tests/unit/analysis/test_audit_canary.py`
- **Why:** the module has **no importer in `src/` at all**. Only its own test imports it. All three functions are test-only.
- **Action:** `git rm` both (per `AGENTS.md` §4 — never `rm` on tracked files).

### B5. Delete the abandoned memory subsystem 🧊
- **Delete:** `src/webhook_agent/core/memory_service.py` (189 lines), `tests/unit/core/test_memory_service.py` (83 lines)
- **Edit:**
  - `core/agent_definition.py` — import at `:35`, init at `:153`, and the `memory_service=` argument at `:289`, `:295`, `:372`, `:378`, `:389`
  - `core/__init__.py` — import at `:46`, `__all__` entry at `:63`
  - `tests/unit/test_worker.py:443-449` — the logger-hierarchy assertion
  - `tests/unit/test_agent_core.py:273` — `agent._memory_service = MagicMock()`
- **Why:** `search_memory` and `add_session_to_memory` have **zero callers in `src/`**, no memory tool is registered, and `Runner(memory_service=...)` is optional. Nothing reads it.
- **Why it's safe:** zero readers means zero behavior change.
- **Caveat:** ADK's `Runner` may call `add_session_to_memory` internally on session end. Even so, nothing reads memory back without a memory tool, so the deletion is behavior-neutral.

### B6. Verification for Phase 1
Per `AGENTS.md` §7, in order:
1. `./scripts/ruff-all.sh`
2. `uv run pytest <each touched test file>`
3. `uv run pytest --tb=short -q`
4. Commit — the pre-commit hook re-runs lint and type checks on staged Python files.

Use `git rm` for B4/B5 deletions so the removal is visible and reversible in review.

**Expected outcome:** ~268 source lines and ~123 test lines removed, zero behavior change.

---

## Phase 2 — Fix the real bug: session scoping

**Agreed design (the maintainer's, which supersedes my earlier proposal):** the PR is the scope; the sender is an attribute.

> ⚠️ **Do not implement my earlier suggestion of a constant `user_id`.** It was wrong: the memory service keys on `(app_name, user_id)`, so a constant would have collapsed memory into one bucket for every PR in the repo.

### C1. Write the failing test FIRST
```python
def test_second_sender_on_same_pr_shares_session_history():
    """Two events, same PR, different senders -> one shared session."""
    # event 1: pull_request.opened,  sender=cgj8702-agents
    # event 2: issue_comment.created, sender=cgj8702, same PR number
    # assert event 2's session contains event 1's review findings
```
- **Why first:** this test settles whether the ADK session key really is the `(app_name, user_id, session_id)` triple. If it **passes today**, the session claim is wrong and `C2`/`C3` should be dropped. If it **fails today**, the bug is confirmed and the test becomes the regression guard.
- **This is the highest-value test in the plan**, because it converts an inference about ADK internals into a fact.

### C2. Make `user_id` PR-derived
- **File:** `src/webhook_agent/core/execution.py:287`
- **From:** `user_id = sender_login or "anonymous"`
- **To:** `user_id = f"{repo_full_name}#{pr_number}"`
- **Why:** makes the session triple stable per PR regardless of who comments, using stock ADK. No custom `SessionService` needed.

### C3. ⚠️ Fix the attribution line (do not skip)
- **File:** `src/webhook_agent/core/execution.py:509`
- **From:** `user_state_map["sender"] = user_id`
- **To:** `user_state_map["sender"] = sender_login`
- **Why:** after `C2`, the current line would silently start recording `"owner/repo#375"` as the *sender*. Exactly the kind of bug that surfaces six weeks later as a confusing mystery.
- **Note:** `user_state_map` is keyed by `user_id` (`:504`), so its keying changes with `C2` — that's intended (per-PR state), but check every reader.

### C4. Optional: custom `SessionService`
A `SessionService` that ignores `user_id` for keying is semantically purer than `C2`, but it's more code to own. `C2` achieves the same behaviour in two lines. **Recommendation: do `C2`.**

### C5. Confirm attribution survives
Verify the sender is still recoverable from:
- ADK's `Event.author`
- `user_state_map["sender"]` (after `C3`)
- the event metadata

### C6. Fix the memory-service keying — only if `B5` was *not* done
If you keep the memory service for future cross-PR learning, its key must include the session:
```python
def _key(self, app_name: str, session_id: str) -> str:
    return f"{app_name}:{session_id}"
```
If `B5` was done, this is moot. **Do not keep it "just in case"** — the keep-or-delete call is decision **P5** (Part 5); record it there rather than deferring.

---

## Phase 3 — Decide the dead features (maintainer's call)

Each of these is a **policy decision**, not a cleanup task. Nothing here gets deleted until the matching `P`-decision is recorded in Part 5.

### D1. Circuit breaker: delete or wire up? → decision **P4**
- **File:** `src/webhook_agent/state/circuit_breaker.py` — **270 lines**
- **Finding:** `CircuitBreaker` is **never instantiated anywhere in the repo**. Its only references are an import and an `__all__` entry in `state/__init__.py`. `call_async()` (53 lines) has zero callers. Nothing imports `webhook_agent.state` as a package; the three live consumers import `state.review_checkpoint` / `state.review_idempotency` directly.
- **Two honest options:**
  - **Delete it.** You have retries (`worker.py:81` `_is_transient` + `_RETRY_DELAYS` with jitter). If that's sufficient, 270 lines go.
  - **Wire it up.** If you actually want circuit breaking around GitHub API calls, instantiate it and route calls through `call_async`. That's real work and it's a *feature*, not cleanup.
- **Do not leave it as-is.** It's a docstring promising resilience you don't have — the exact thing that makes the repo untrustworthy to read.

### D2. Thread-resolution tools: delete or gate? → decision **P2**
- **Files:** `tools/github_tools.py:482` `resolve_review_thread` (~29 lines), `:513` `resolve_bot_review_threads` (39 lines)
- **Finding:** exported in `tools/__init__.py` (`__all__` + lazy-import set), registered on **no agent**, called by nothing but `tests/unit/github/test_thread_resolver.py`.
- **Options:**
  - **Delete both** + their exports + their test. Cleanest.
  - **Keep, and gate behind Phase 4 policy** (`may_resolve_threads`, default `False`). Then the blueprint's `REVIEW_THREAD_COLLABORATION` idea becomes a one-line policy flip *if you ever want it*.
- **⚠️ If you ever do give the model `resolve_review_thread`:** it lets the bot resolve **its own review threads** — a self-grading loop. `SECURITY.md` must say so explicitly.

### D3. Remove the dead parameters from the abandoned memory project
- `build_repo_profile_context(..., memory_bank_heuristics=None)` — never supplied; the only call site (`prompts.py:554`) passes just the repo name. Either delete the parameter or actually populate it.
- `add_events_to_memory(..., session_id=None)` — accepted, written into metadata, never used for scoping. Moot if `B5` is done.

### D4. ⚠️ Investigate: two of three scorecard dimensions appear unwired → decision **P6**
- **File:** `src/webhook_agent/review/review_enforcer.py`
- **Finding:** three dimension predicates — `is_contract_dimension` (`:34`), `is_security_dimension` (`:39`), `is_test_coverage_dimension` (`:45`). Only **`is_contract_dimension` is exported and used**. The other two have zero references in `src/` and are exercised only by their own tests. All three have near-identical tests (0.98 structural similarity).
- **This may be a real bug, not dead code:** the scorecard floor may only be enforcing one of the three dimensions it was designed for. **Investigate before deleting.**

### D5. Remaining test-only functions (120 lines)
Referenced only by their own tests, never by a code path. Delete or wire:

| Lines | Location |
|--:|:--|
| 29 | `models/model_chain.py:215` `_get_model_tpm_limit()` |
| 19 | `models/model_chain.py:246` `_count_tokens_exact()` |
| 6 | `models/review_budget.py:53` `should_suppress_round()` |
| 5 | `review/schemas.py:327` `has_genuine_summary_risk()` |
| 4 | `review/review_enforcer.py:39` `is_security_dimension()` ← see D4 |
| 4 | `review/review_enforcer.py:45` `is_test_coverage_dimension()` ← see D4 |

Note: `_count_tokens_exact` overlaps `count_tokens_exact` in `core/agent_definition.py:116` (which *is* exported). Check whether one supersedes the other.

### D6. Fix the lying test
- **File:** `tests/unit/test_worker.py` — `test_suppress_bot_actor_by_type`
- **Problem:** docstring says *"Bot events detected via `sender.type == 'Bot'` should be suppressed"* but the assertion is `is True` (allowed), and the inline comment says the opposite of the docstring.
- **Why it matters:** in a suite this size, a handful of tests that misdescribe themselves cost more during triage than 200 redundant tests ever would. This is the only genuine *test debt* found.

### D7. Tier 6: 32 dead re-exports — handle carefully
Package `__init__.py` files re-export 32 names that **no module imports** (`ASTAnalysisResult`, `CallSiteVisitor`, `RateLimitExceededError`, `PRClosedRegistry`, `set_active_environment`, `_MAX_RETRIES`, …).

- **Important:** most of these are **not dead code** — they're used internally within their own module. It's the *re-export* that's dead.
- These `__init__.py` facades are the last remnants of the shim era (shims were retired in #314/#249 but the facades stayed).
- **Do not delete blindly.** Treat as a separate, later cleanup with its own review.

---

## Phase 4 — The policy layer (the actual fix)

This is the change that ends the oscillation. Everything else is cleanup that makes this easier to land.

### E1. Create `src/webhook_agent/policy.py` (~120 lines)
One file. One enum, one dataclass, one pure function.

```python
class Mode(StrEnum):
    FORMAL_AUDIT  = "formal_audit"    # PR opened/synced, /review intent
    THREAD_COLLAB = "thread_collab"   # reply on an inline review comment
    ISSUE_TRIAGE  = "issue_triage"    # issue opened, delegation request
    DISCUSSION    = "discussion"      # general chatter on a PR/issue
    IGNORE        = "ignore"          # bot event, closed PR, mutations off

@dataclass(frozen=True)
class EventPolicy:
    mode: Mode
    # what may be written to GitHub
    may_submit_review:    bool
    may_post_comment:     bool
    may_resolve_threads:  bool   # False today
    may_create_issue:     bool   # False today
    # how the run is configured
    prefetch_diff:        bool
    thinking_budget:      int
    session_key:          str    # "owner/repo#42"  <-- also fixes the Phase 2 bug
    reason:               str    # why this mode, for logs

def resolve_event_policy(event_data: dict, *, dry_run: bool) -> EventPolicy:
    """Single source of truth. Pure, no I/O, trivially testable."""
```

**Pure means:** no GitHub client, no network, no time. Same input → same output. Which makes it the easiest thing in the repo to test — a table of inputs and expected modes.

### E2. Point the seven call sites at it
Each currently re-derives the answer independently (see §3.4):

| Site | Today | After |
|:--|:--|:--|
| `execution.py:296` | `_is_formal_review_eligible(...)` | `policy.mode is Mode.FORMAL_AUDIT` |
| `pr_context.py:32` | `_should_prefetch_diff(...)` | `policy.prefetch_diff` |
| `ci_gate.py:28` | `GATED_EVENTS` | `policy.mode` |
| `model_chain.py:175` | canonical checks | `policy.thinking_budget` |
| `prompts.py:269` | canonical checks | `policy.mode` |
| `execution.py:416` | runner ternary | `policy.mode` |
| `processor.py` routing | `route_event` + gates | `policy` |

**`evaluate_writeback_policy` gets absorbed, not duplicated.** Its closed-PR / bot-event / dry-run / mutations-disabled checks become the branches that produce `Mode.IGNORE`.

**Net line count should go down.** If it goes up, something's wrong.

### E3. Real tool gating in `before_tool_callback`
This is the part that replaces "the tool list is the policy":

```python
_TOOL_POLICY: dict[str, Callable[[EventPolicy], bool]] = {
    "read_file":               lambda p: True,
    "search_files":            lambda p: True,
    "get_issue":               lambda p: True,
    "list_open_pull_requests": lambda p: True,
    "review":                  lambda p: p.may_submit_review,
    "resolve_review_thread":   lambda p: p.may_resolve_threads,
    "create_issue":            lambda p: p.may_create_issue,
}
```
```python
policy = ctx.state["event_policy"]
check = _TOOL_POLICY.get(tool.name)
if check is None or not check(policy):
    return f"Error: '{tool.name}' is not permitted in mode '{policy.mode}'."
```

### E4. Make it fail-closed, with a test to prove it
An unknown tool is **denied**, not allowed. And the guard that keeps it honest:

```python
def test_every_registered_tool_has_a_policy_entry():
    """A tool registered on any agent without a policy entry is a security hole."""
    registered = {...all tools across all agents...}
    assert registered <= set(_TOOL_POLICY), registered - set(_TOOL_POLICY)
```

### E5. Add `list_open_pull_requests` (the one genuinely good idea in the blueprint)
- Read-only, low risk, and it directly serves `AGENTS.md` §8 ("check `gh pr list` for overlapping work before delegating").
- Gate it as always-allowed.

### E6. Generate the `SECURITY.md` tool table from `_TOOL_POLICY`
Then the docs cannot drift from the code. This closes the `AGENTS.md` §8 vs `SECURITY.md` §5 contradiction permanently.

### E7. Design constraints — do NOT violate these
- **No YAML/JSON config file.** Policy in Python, next to the types. A config file means no type checking, no refactor safety, and a new thing to keep in sync.
- **No plugin system.** No `PolicyProvider` ABC, no registry of registries.
- **No new package.** One file at `src/webhook_agent/policy.py`.
- **Don't gate read-only tools.** Gating `read_file` buys nothing and creates noise.
- **Keep it under ~200 lines.** If it grows past that, push back on it — that's the whole point.

### E8. The three rules that prevent re-sprawl
1. **Events collapse into modes; modes carry permissions.** Never let events carry permissions directly. (40+ event types → 5 modes → permissions. That caps the real surface at ~35 cells, and ~15 are ever non-trivial.)
2. **Unknown event → no write permissions.** Fail closed. Don't try to enumerate every GitHub event; the existing fallthrough is fine.
3. **Don't invent modes you don't need.** The right number is however many *distinct permission sets* you have. If two modes have identical permissions, they're one mode. Five is a guess, not a target.

### E9. The acceptance test for this whole plan
> If someone asks *"how do I change whether the bot may resolve a review thread?"* — can you point at **one file** and change a line?

Until that's yes, the oscillation will resume, no matter how many agents exist.

---

## Phase 5 — Naming and clarity (cheap, high morale payoff)

Pure renaming. No behaviour change. Listed deliberately: a misleading name is a real cost, because it shapes how the whole system gets read.

### F1. Rename the two agents to what they actually are
- `_code_auditor` → `reviewer`
- `_conversational_agent` → `collaborator`
- **Why:** they're two **roles** with different capabilities, not two halves of a broken brain.

### F2. Stop calling it "split-brain" in docs
- **Files:** `docs/slash_commands_catalog.md`, `README.md`, anywhere else.
- **Why:** the term implies a defect. There isn't one. Two roles sharing a session service is a design, not an injury.

### F3. Keep two agents — do NOT merge (unless after Phase 4)
The blueprint's case for merging rests on a **false premise** ("No Shared Memory"). The real difference between the two agents is:
- tool difference is `review` vs `get_issue` — and `get_issue` is inert
- thinking budget (1024 vs ≤512)
- instruction text (7,079 chars vs 2,763)

So the split is over-engineered for a *one-tool* difference — but it also works, and merging buys almost nothing. **After Phase 4 it becomes a mechanical change with no security implications.** Until then, merging would just re-split later, as it has before.

---

## Phase 6 — Test hygiene (opportunistic only)

### G1. Fix the lying test
See `D6`. One line + docstring. ~10 minutes.

### G2. Collapse duplicate families ONLY when already editing that file
- 74 near-duplicate families, 195 tests, ~121 collapsible functions (819 → ~698).
- **This is a byproduct of doing normal work, not a project.**
- **Never open a PR titled "refactor the test suite."** That's how you get a 5,000-line diff nobody can review.

### G3. Add `--durations=15` when you next run the suite
So you know which tests actually cost you time.

### G4. The measurement rule
Measure **ratios, not counts**. 824 tests sounds catastrophic; 1:1 against source is normal.

---

## Phase 7 — Docs (same PR as the behaviour change, never after)

Per `AGENTS.md` §7 and the T-15 precedent, doc drift earns its own remediation task if you let it accumulate.

### H1. Update in the same PR as the change
- `SECURITY.md` — the authoritative tool/policy table (generated from `_TOOL_POLICY`)
- `docs/slash_commands_catalog.md` — documents the dual-engine model in a table; wrong the moment Phase 4 lands
- `README.md`
- `AGENTS.md` §8 — the "API-created issues" sentence
- `docs/templates_catalog.md` — if tool inventory changes

### H2. Add a short "What this system does NOT do" section
List the things that don't exist, so no future reader (human or agent) builds a false model:
- no circuit breaking (unless D1 wires it up)
- no long-term memory (unless you keep it)
- no code mutation
- no label management

### H3. Retire the `__init__.py` facades — separately, later
See `D7`. 32 dead re-exports. Its own PR with its own review.

---

# Part 5 — Decisions only you can make

Nothing in Phase 3 or Phase 4 should start before the relevant decision is recorded here. Record the answer, then date it. An answered question stops being re-litigated.

| # | Decision | Options | Blocks | Answer |
|:--|:--|:--|:--|:--|
| **P1** | May the bot create issues? | yes (gated, no `labels` param) / no (delete AGENTS.md §8 sentence) | `A3`, `E3` | *(open)* |
| **P2** | May the bot resolve review threads? | no (delete both tools) / gated off by default | `D2` | *(open)* |
| **P3** | May the bot add labels / merge / commit? | almost certainly no | `A3` | *(open)* |
| **P4** | Circuit breaker: delete or wire up? | delete (270 lines) / wire it (a feature, not cleanup) | `D1` | *(open)* |
| **P5** | Keep the memory service for future cross-PR learning? | delete (recommended — it's unread) / keep + fix key | `B5`, `C6` | *(open)* |
| **P6** | Is the unwired security/test-coverage scorecard dimension a bug? | investigate first | `D4` | *(open)* |
| **P7** | Merge the two agents into one? | not now; revisit after `E1`–`E4` | `F3` | *(open)* |
| **P8** | Remove the `check_run` subscription? | remove (recommended — already suppressed in code, so it is pure cost) | §8.3 | *(open)* |

**Already decided in-session (recorded for the record):**
- **Session scope = PR, not sender.** The PR is the scope; the sender is an attribute. `session_id = "owner/repo#N"` is correct and stays.
- **Reject the `Unified Autonomous Pair Programming Agent` blueprint.** Its memory premise is false and its tool additions reverse deliberate decisions.
- **The policy layer is the right direction**, with the constraints in `E7`.

---

# Part 6 — Offered but not yet taken up

Loose ends from the session. Any can be picked up independently.

1. **The 74 near-duplicate test families as a worklist file.** A finite checklist instead of a vague dread. (Offered; not taken.)
2. **The failing session test (`C1`).** Offered; not taken. Still the highest-value single artifact in this plan.
3. **The Phase 1 deletion PR (`B1`–`B6`).** Offered; not taken. ~390 lines removed, zero behaviour change — the best possible first PR.
4. **Implementing `policy.py` (`E1`–`E4`).** Offered; not taken.
5. **The gcloud log half of the walkthrough.** Blocked on environment: this sandbox has no `gcloud`, no Google SDK, no GCP credentials, and PyPI is unreachable. **You need to run it.** For PR #375's window:

```bash
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/python" AND timestamp >= "2026-10-17T14:24:00Z" AND timestamp <= "2026-10-17T14:27:00Z"' \
  --project=cgj8702-webhook-agent --order=asc --limit=200

# narrow to one station
... AND labels.python_logger="processor"
... AND labels.python_logger="webhook_agent.core.execution"
```

Two gotchas from `.agents/skills/gcloud-logging`: relative offsets like `"-15m"` are **rejected inside the filter expression** (use `--freshness` instead), and most interesting lines are `logger.debug(...)` — silently dropped by GCP unless logging is set to `DEBUG`.

6. **A second walkthrough of the two foggiest stations.** Still unidentified: `analysis/` (what the model actually receives) or `review/grounding.py` (989 lines, the real remaining complexity).

---

# Part 7 — Guardrails against recurrence

These are the rules that, had they existed, would have prevented this. Cheap to adopt, high leverage.

1. **Every capability decision lives in one file.** If changing "may the bot do X?" requires touching more than one file, the policy layer is missing something.
2. **A document's premises get verified before its conclusions get adopted.** The blueprint nearly caused a rewrite on a false memory claim. Check the load-bearing facts first.
3. **Never let structure carry policy.** Topology and tool lists are implementation. Policy is intent. Keep them separate.
4. **Measure ratios, not counts.** Counts alarm; ratios inform.
5. **Read the history before believing the structure.** 738 commits explain more about *why* the code looks like this than the code does. `git log -S '<symbol>'` and `git log -- <path>` are the tools.
6. **One logical change per PR** (`AGENTS.md` §4). The cleanup above is ~8 PRs, not 1.
7. **`git rm` for tracked file deletions** (`AGENTS.md` §4) — never `rm`. Removals must be visible and reversible in review.
8. **Delete abandoned features rather than leaving them documented.** A dead feature that reads as live is worse than no feature, because it corrupts the reader's model.
9. **When overwhelmed, ask for a *narrower* view, not a broader one.** One path, one file, one station.
10. **Verify, don't assume** (`AGENTS.md` §7) — including about your own repo's health. The tests were fine; the *feeling* wasn't.

---

# Part 8 — Event surface & observability

> **Label note:** you referred to these as plans "A–F". They're prefixed `EV-` here because Phase 0 already uses `A1`–`A4`, and this document has already caused one confusing collision (`B5` vs `Part 5`).

Discovered by reading live gcloud logs on 2026-10-17. The worked example is the `workflow_run` bug.

## 8.1 The worked example: one cause, four faces

`workflow_run` was referenced **nowhere** in `src/`:

```
$ grep -rn 'workflow_run' src/ --include=*.py
(nothing)
```

Those three actions (`requested`, `in_progress`, `completed`) arrived, passed every filter, spawned a full agent run, and no code path read them. In a 52-minute log window that produced **6 complete agent runs for pure noise**:

| `workflow_run` | Gemini call | Orphan session |
|:--|:--|:--|
| 15:37:10.303 `requested` | trace `8900` → "Hello! 👋 Hannibal Hub…" | `9bbfc4f0-ca40-…` |
| 15:37:13.407 `in_progress` | trace `8d79` → "Hello! Hannibal Hub's…" | `9d6fe5f0-ca40-…` |
| 15:37:44.378 `completed` | trace `ab05` → "Hello! 👋 I'm Hannibal…" | `b1353d10-ca40-…` |
| 16:28:19.857 `requested` | trace `1b45` → "Hello! Hannibal Hub's…" | `c0fab2a0-ca47-…` |
| 16:28:22.717 `in_progress` | trace `5553` → "Hello! I am Hannibal…" | `c2ced660-ca47-…` |
| 16:29:43.170 `completed` | trace `bf28` → "Hello! I'm Hannibal Hub's…" | `e9e9d2e0-ca47-…` |

Six self-introductions, six orphaned sessions, perfectly 1:1.

### The causal chain

1. `build_transient_turn_content` (`prompts.py:195-278`) has branches for `issue_comment.`, `issues.`, `pull_request.`, `pull_request_review_comment.`, `pull_request_review.` — and **nothing** for `workflow_run.*`. So the entire prompt the model receives is three lines of metadata with **no ask**.
2. The conversational agent's instruction says *"Engage conversationally… respond with warmth."*
3. A model told to be warm, handed an empty prompt, fills the silence with a greeting.
4. `_post_conversational_reply` discards it, because `pr_number` is `None` (`execution.py:219`).
5. `_derive_session_id` (`agent_definition.py:403-406`) found no `issue.number` and no `pull_request.number`, so it fell back to `f"{repo_name}/{delivery_id}"` — a **GitHub delivery GUID**, guaranteed unique, therefore guaranteed to create a new session every time.

### Why this is the answer to "let the agent decide if action is needed"

The original design intent was that the agent would read the payload and determine whether any action was necessary. The logs are the empirical refutation: **given an empty prompt, the agent does not decide "no action needed" — it invents a social response.** Not because the model is weak, but because "is anything needed here?" is not a question you can ask a language model with a prompt containing nothing, while also instructing it to be responsive.

**The no-op decision must be deterministic, and must happen before the model is called.** That is precisely what `Mode.IGNORE` in the policy layer exists for.

## 8.2 The cost hierarchy — the generalisable lesson

Suppression inside `should_process_event` is **late**. By the time it fires, the event has already travelled:

```
GitHub → Cloud Run invocation → Pub/Sub publish → worker pull → decode → filter
```

You paid for all of that. The cheapest filter is not subscribing.

| Level | Where | Cost per event |
|:--|:--|:--|
| **1** | Don't subscribe (GitHub App settings) | **zero** |
| **2** | Router drops it (not in this repo) | 1 Cloud Run invocation |
| **3** | `should_process_event` | full Pub/Sub + worker + logs |
| **4** | `_dispatch_event`, before the agent | + no Gemini call |
| **5** | Let the agent decide | Gemini call, every time |

`workflow_run` was sitting at **level 5** for events that should never have been at level 1. `check_run` sits at **level 3** — cheaper, but still not level 1.

**Rule:** for every subscribed event type, ask which level it *should* be at, then move it there. Every event that reaches level 5 without a reason is a bug.

## 8.3 Subscription audit (CI events)

| Event | Subscribed? | Used by code? | Verdict |
|:--|:--|:--|:--|
| `check_suite` | **yes — keep** | `processor.py:638` → `_handle_ci_completed`; the only CI event acted on | **Required.** It is the CI-gate trigger. |
| `check_run` | remove | no — only appears in the suppression tuple and a docstring | **Remove the subscription.** Already dropped in code, so it is pure webhook + Pub/Sub + worker cost for nothing. |
| `status` | **not subscribed** (correct) | the *event* is unused; the *data* is still read via REST (see 8.6) | Leave unsubscribed. |
| `workflow_run` | removed ✅ | never referenced anywhere | Done. |

### The semantic fact that matters — and why it means you need only `check_suite`

GitHub's own docs claim: *"A GitHub App only receives one `check_suite` event per commit SHA."* **That is false.** Filed as `github/docs#21435`, with evidence:

```
$ curl '.../commits/<sha>/check-suites' | jq '.check_suites[].app.id'
15368
15368
15368
15368      ← four check suites, all from GitHub Actions
```

**GitHub Actions creates one check suite per workflow run.** So a commit with 3 workflows gets 3 check suites → **`check_suite.completed` fires 3 times per commit**, and the first firing is usually premature.

### Your design already handles this correctly — do not "simplify" it

```python
# processor.py:542 — _handle_ci_completed docstring
"""A failed suite is ignored (silent skip). A passing one re-checks the whole
commit, since other suites may still be running or may have failed."""
```

And `evaluate_ci` does not trust the suite payload — it recomputes globally:

```python
# ci_gate.py:75-90
for run in commit.get_check_runs():          # ALL runs, ALL suites on the commit
    ...
combined = commit.get_combined_status()      # plus legacy statuses
```

Flow: suite #1 completes → re-evaluate → suite #2 still pending → `PENDING` → no review. Suite #3 completes → re-evaluate → all green → review. **The event is only a trigger; the verdict is recomputed from the API.** That is why `check_run` adds nothing — it would fire per *job* instead of per *workflow*, recomputing the identical verdict more often.

## 8.4 Plan EV-A … EV-F

| ID | Action | Status |
|:--|:--|:--|
| **EV-A** | Unsubscribe `workflow_run` in GitHub App settings | ✅ **done** |
| **EV-B** | Add code-level guards so unsubscribed CI events can never reach the agent | ⬜ to do — see 8.5 |
| **EV-C** | Never key a session on a delivery GUID (`_derive_session_id` fallback) | ⬜ to do — see 8.7 |
| **EV-D** | Move the no-op decision *before* the agent (deterministic `Mode.IGNORE`) | ⬜ to do — this is `E1`–`E4` in Phase 4 |
| **EV-E** | Collapse the logging to one line per lifecycle stage, owned by one place | ⬜ to do — see 8.8 |
| **EV-F** | Fix the logger bindings so `python_logger` matches the file it is in | ⬜ to do — see 8.8 |

**Relationship to Phase 2 (`C1`–`C3`).** `EV-C` and Phase 2 both touch session identity but are different fixes:

- **Phase 2 `C1`–`C3`** fixes `user_id` so sessions are **PR-scoped and shared by every sender** on that PR.
- **`EV-C`** fixes `session_id` so events with no PR number stop minting a **new session per delivery**.

Fix only one and you still have half the bug: `C2` alone will not help a `workflow_run` event (it has no PR number to key on), and `EV-C` alone will not let two different humans share one PR's history.

**Ordering:** land **`EV-D` first**. If the agent never runs for a no-op event, no session is created at all, which removes most of `EV-C`'s blast radius. `EV-C` then remains as a guard against a *future* event type silently minting per-delivery sessions.

## 8.5 `EV-B` — the code fix (tuple + comment)

**Current** (`processor.py:530-537`):

```python
        # Ignore automated CI infrastructure noise, installation lifecycle events, and review thread state changes
        return event_name not in (
            "check_suite",
            "check_run",
            "status",
            "installation",
            "pull_request_review_thread",
        )
```

**Replace with:**

```python
        # CI infrastructure events. We subscribe to check_suite only — its `completed`
        # action drives the CI gate (see review/ci_gate.py). check_run / status /
        # workflow_run / workflow_job are NOT subscribed, and are listed here as
        # guards so they can never reach the agent if a subscription is ever re-added.
        # No code path reads their payloads, and build_transient_turn_content has no
        # branch for them — so reaching the agent means an empty prompt and a
        # hallucinated reply (see docs/plans/repo_reclamation.md §8.1).
        return event_name not in (
            "check_suite",
            "check_run",
            "status",
            "workflow_run",
            "workflow_job",
            "installation",
            "pull_request_review_thread",
        )
```

**Why add to the tuple rather than trim it.** The current tuple lists three event types you do not subscribe to (`check_run`, `status`, and until recently `workflow_run`) — which looks like dead code. It is not. It is a **tripwire**, and `workflow_run` proves it: your fix for that bug lives *entirely* in GitHub App settings, with **no code-level guard**:

```
$ grep -rn 'workflow_run\|workflow_job' src/ --include=*.py
(nothing)
```

So if that subscription ever returns — a reinstall, an org-level App config change, someone debugging CI — the events flow straight through again: `should_process_event` returns `True`, `build_transient_turn_content` has no branch, and you get self-introductions and orphaned sessions again.

**The distinction from the circuit breaker / memory service deletions.** Those were unreachable *features* whose docstrings promised capability that did not exist — which corrupts a reader's model. These are unreachable *guards*: they claim nothing, and they prevent a known failure. Unreachable features are harmful; unexercised guards are just unexercised. The comment is what makes their status explicit — not deletion.

**Cost of keeping them:** a tuple membership test on events that never arrive. **Benefit:** the guard is version-controlled and reviewable, instead of living in a settings page nobody diffs.

## 8.6 Do NOT do these (three traps)

### Trap 1 — do not remove `get_combined_status()` from `ci_gate.py`

There are **two unrelated things called `status`**:

| | Where | What it is | Remove? |
|:--|:--|:--|:--|
| The `status` **webhook event** | `processor.py:534` (tuple) | An event you do not subscribe to | Keep as a guard (8.5) |
| The commit-status **data** | `ci_gate.py:83` `commit.get_combined_status()` | A REST read, independent of any subscription | **NO** |

```python
# ci_gate.py:83-90
combined = commit.get_combined_status()
if getattr(combined, "total_count", 0):
    state = (getattr(combined, "state", "") or "").lower()
    if state in FAILING_STATUS_STATES:      # {"failure", "error"}
        return CIState.FAIL                 # ← blocks the review
```

That is an **API read**, not event handling. Not being subscribed to the `status` *event* does not mean there are no legacy statuses on the commit — any third party can post one via the Status API, and this reads them regardless.

**It fails closed and deleting it fails open.** It returns `CIState.FAIL`, which *blocks* a review. Remove it and a commit carrying a failing legacy status could be `APPROVE`d. Today it is a no-op (`total_count == 0`, so the `if` skips) and costs one API call per evaluation — cheap insurance.

### Trap 2 — do not move the `check_suite.completed` early-return below the bot filter

```python
# processor.py:379-382
if ev.get("event_name") == "check_suite" and ev.get("action") == "completed":
    # Finished CI drives deferred reviews (see review/ci_gate.py). Its sender is
    # usually github-actions[bot], which the bot-sender filter below would drop.
    return is_ci_gate_enabled()
if _is_bot_event(ev):
    return False
```

GitHub sends the **`ghost` user** as `sender` for check events with no authenticated actor (its docs call this out explicitly for `check_run` and `check_suite`). So the sender is unreliable for these events, and the early-return placement — *before* `_is_bot_event` — is deliberate. A naive reorder would silently break the CI gate.

### Trap 3 — do not remove `"status"` / `"check_run"` from the tuple

See 8.5. They are guards, not dead code. Removing them makes a future re-subscription fail loudly in production instead of being caught here.

## 8.7 `EV-C` — never key a session on a delivery GUID

```python
# agent_definition.py:403-406
number = issue.get("number") or pr.get("number")
if number:
    return f"{repo_name}/{number}"
return f"{repo_name}/{event_data.get('delivery_id', 'unknown')}"   # ← GUID
```

A GitHub delivery ID is a GUID — **guaranteed unique, therefore guaranteed to miss the cache and create a new session every time.** Confirmed in the logs: `…/b1353d10-ca40-11f1-…` is a delivery GUID, not a PR number.

The proof it is this mechanism and nothing else: the `pull_request.synchronize` review at 15:37:50 used `repo/375`, found the existing session, and logged **no** "Created new ADK session" line. Stable key → reuse. GUID key → create.

**Consequence:** `InMemorySessionService` has **no eviction** — unlike `_processed_deliveries`, which does have a FIFO bound (`processor.py:613-616`). Sessions accumulate forever, each holding full event history.

**Fix:** return a stable per-repo key for non-PR events, or skip session creation entirely for events that will do nothing. Do not invent a new key from a per-delivery value.

### Pre-existing risk worth recording: the `status` stall

Because you are not subscribed to `status`: if a legacy status is the **last** thing to go green on a commit, no event fires, and the deferred review **stalls until the next push**. This risk exists today, independently of everything else here. It is fine while CI is pure Actions — but record the assumption so a future CI change does not silently break the gate. (This is also why `get_combined_status()` still earns its keep: it correctly sees a failing status during the *next* evaluation triggered by a `check_suite`.)

## 8.8 `EV-E` / `EV-F` — logging

**`EV-E`: one no-op event currently emits eight INFO lines.**

| # | Line | Logger label | Message |
|--:|:--|:--|:--|
| 1 | `processor.py:618` | `.processor` | `Event processed: workflow_run.requested` |
| 2 | `processor.py:654` | `.processor` | `Agent starting execution for repo X` |
| 3 | `processor.py:179` | **`.core`** | `🧠 Processing event via ADK agent (trace…)` |
| 4 | `callbacks.py:67` | `.core.callbacks` | `🤖 [SubAgent: conversational_agent] Starting…` |
| 5 | `execution.py:455` | `.core.execution` | `Created new ADK session …` |
| 6 | `execution.py:119` | `.core.execution` | `🧠 Agent response: Hello!…` |
| 7 | `execution.py:749` | `.core.execution` | `🏁 Agent completed with no actions` |
| 8 | `processor.py:911` | `.processor` | `Agent completed execution with no actions taken` |

**Three** "we started" signals (#2, #3, #4). **Two** "nothing happened" signals (#7, #8). Plus hidden DEBUG twins: `processor.py:173` is a near-verbatim duplicate of `:179`, and `execution.py:114` duplicates `:119`.

**The real defect is unjoinability, not verbosity.** No single line carries the event, the trace, and the outcome together:

- `:618` has the event — no trace, no repo
- `:654` has the repo — no event, no trace
- `:911` has the repo — no event, **no trace** ← the line that says "nothing happened" cannot tell you *what* did nothing
- the trace appears only in `:179`, `execution.py:119`, `:749`

So answering "which event did nothing, and why?" requires timestamp archaeology.

**`EV-F`: the logger-label trap.** `processor.py:173/179` log via `core_logger`, so GCP labels them `python_logger="webhook_agent.core"` even though the code physically lives in `processor.py`. A gcloud filter of `labels.python_logger="processor"` **silently drops those two lines** — which is exactly the mistake made earlier in this session when writing log-query guidance. Check the logger binding, not the file path.

**Fix:** one line per lifecycle stage, owned by one place, with every line carrying `event=`, `trace=`, `repo=`, `outcome=` as structured fields (via `extra={...}`) rather than string interpolation — so GCP can filter on `jsonPayload` fields instead of regex. Delete the DEBUG twins or demote the INFO versions to DEBUG.






# Appendix A — The map

Five stations on one conveyor belt. Every package belongs to exactly one.

| # | Station | Code | Lines |
|:--|:--|:--|--:|
| 0 | **Edge** | GitHub → Cloud Run router → Pub/Sub. **Not in this repo** (`SECURITY.md` §1: signature verification happens in a router that "cannot be audited here"). | — |
| 1 | **Event arrives** | `worker.py` (406) + `processor.py` (914) — **the whole spine, 1,320 lines** | 1,320 |
| 2 | **Get context** | `github/` — diff, PR state, auth, bot identity | 1,295 |
| 3 | **Analyze deterministically** | `analysis/` — AST, symbol graph, test impact, diff filter, lockfiles, security scanners. **No LLM.** | 2,761 |
| 4 | **Think** | `core/` — the only clever station. `agent_definition.py` + `execution.py` + `prompts.py` = **~1,800 lines** | 3,057 |
| 5 | **Write back** | `review/` — format, ground, submit. **A third of the whole codebase.** | 6,723 |

Plus: `models/` (1,566 — chains, rate limits, factory), `state/` (834 — checkpoints, idempotency), `environment/` (588 — PR workspaces).

**The thing to internalise:** the thinking core is ~1,800 lines. That's the part worth understanding cold. The rest is plumbing and deterministic analysis you can ignore until it breaks.

---

# Appendix B — Suggested PR sequence

Eight small PRs, in order. Each independently reviewable and revertible.

| PR | Contents | Risk | Size |
|:--|:--|:--|:--|
| **0** | `EV-B` guard `workflow_run` / `workflow_job` in the suppression tuple + comment (the code half of `EV-A`, which is already done) | none | +6 lines |
| 1 | `D6` fix the lying test | none | 1 line |
| 2 | `B1`–`B3` dead functions (`normalize_event_type`, `_create_fallback_agent`, `defang_content`) | none | −39 |
| 3 | `B4` delete orphaned `audit_canary` module + its test | none | −40 src, −test |
| 4 | `B5` delete abandoned memory subsystem | none | −189 src, −83 test |
| 5 | `C1`–`C3` session scoping fix + `EV-C` session-key guard (test first) | low | +test, few lines |
| 6 | `D1`–`D3`, `D5` decisions applied | low–medium | depends on **P1**–**P5** |
| 7 | `E1`–`E6` the policy layer | medium | ~+120, net negative |
| 8 | `F1`–`F2`, `H1`–`H2` rename + docs | none | — |
| 9 | `EV-E` / `EV-F` collapse the logging + fix logger bindings | none | — |

**Why this order:** PR 0 is the code half of a fix already started in GitHub settings — cheapest possible first PR. PRs 1–4 are pure deletions and build confidence. PR 5 fixes the one real bug. PR 6 clears the ground. PR 7 is the structural fix, landing on clean ground. PR 8 makes the result readable.

**Do not start PR 7 before PRs 2–4.** The policy layer's whole job is to make the codebase honest; layering it on top of dead features defeats the purpose.

---

*Drafted from the session that produced it. Numbers were measured, not estimated. Where a claim depends on ADK internals I could not execute (no venv, PyPI unreachable), it is marked as an inference with the test that would settle it.*









