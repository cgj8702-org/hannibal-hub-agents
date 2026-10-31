# Forensic Production Log Audit: Fine-Toothed Analysis of Past 8 Hours

**Audit Scope:** 577 Cloud Logging Entries (`2026-10-27T19:22:19Z` to `2026-10-28T03:22:19Z`)  
**Data Sources:** `cgj8702-webhook-agent` (Cloud Run Router, Python Worker, Audit) & `chatbot-project-hannibal` (GCE Host)  
**Artifact Path:** `logs_past_8h.json` / `webhook_agent_logs.json`

---

## 1. Executive Summary: The Real Reality of the System

A fine-toothed chronological review of all 577 log entries demonstrates that the agent is not suffering from isolated edge cases, but from **interconnected architectural feedback loops**. 

When a PR or Issue event arrives, multiple subsystems fight each other:
1. **The Investigation Gate traps the agent in a zombie loop** even after a review has already been successfully published to GitHub, burning 400k+ tokens while idling in 54s sleep pauses.
2. **Deterministic pre-audit findings are silently lost** due to a state initialization race condition in `execution.py`, causing `review_enforcer.py` to forcibly downgrade valid review scorecards to `UNKNOWN`.
3. **The Grounding Verifier rejects 100% of initial PR reviews**, forcing adversarial retries that collide directly with the 250k free-tier TPM ceiling.
4. **Session discovery crashes on every relevant event** with an HTTP 404 because the session extraction regex greedily captures the repository organization name (`cgj8702-org`).
5. **Issue events get misrouted into `formal_audit`**, attempting to submit Pull Request reviews against Issues and throwing 404s.
6. **Continuous deployment restarts (SIGTERM)** kill in-flight workers mid-review, causing Pub/Sub message redelivery, duplicate reviews, and orphaned `/tmp/hannibal-sandbox-*` workspaces.

---

## 2. Deep Dive: The 8 Critical Architectural Bugs Uncovered

### 🚨 Bug 1: The Post-Review Ghost Investigation Loop
* **Log Evidence (Logs [549]–[576])**:
  ```text
  [549] 03:18:56Z | webhook_agent.review_enforcer | Submitted formal review [repo=..., item_id=478, verdict=APPROVE, sha=80a8cc6...]
  [550] 03:18:56Z | webhook_agent.core.execution  | 🔍 [Investigation Gate] Model attempted review on code changes without using investigative tools (trace: e117). Enforcing multi-turn exploration turn 2/5...
  [556] 03:19:49Z | webhook_agent.core.callbacks  | 📊 [LLM Token Usage] model=gemini-3.5-flash-lite, total=82706, prompt=82690
  [560] 03:19:54Z | hannibal_rate_limiter         | TPM THROTTLE: Active 330378/250000. Waiting 54.4s...
  [561] 03:19:54Z | hannibal_rate_limiter         | TPM HARD CEILING: Finalized 330378/250000. Forcing 52.8s wait...
  [566] 03:20:57Z | hannibal_rate_limiter         | TPM THROTTLE: Active 327356/250000. Waiting 54.0s...
  [575] 03:21:57Z | hannibal_rate_limiter         | TPM HARD CEILING: Finalized 258674/250000. Forcing 53.2s wait...
  ```
* **Code Location**: `src/webhook_agent/core/execution.py:217-243`
* **Mechanism**:
  - The model called `review()` on turn 1, and the review was successfully submitted to GitHub!
  - But line 218 checks: `has_investigated = any(r.tool in investigative_tools for r in results)`.
  - Because the model didn't run `search_files` or `read_file` *prior* to `review()`, `has_investigated` was `False`.
  - The `while not has_investigated and turn < max_investigation_turns:` loop in `execution.py` **does not check if `review()` was already submitted**!
  - It sent a follow-up user directive: *"Mandatory Investigation Gate: You cannot finalize a code review in a single turn without active repository exploration..."*
  - The model was forced into a 5-turn ghost investigation loop for a review that had already posted, burning ~420,000 tokens and stalling in 54s rate-limiter waits while the user watched the logs flood in real time!

---

### 🚨 Bug 2: Scorecard Wiping & `UNKNOWN` Floor Overrides
* **Log Evidence (Logs [520]–[526])**:
  ```text
  [520] 03:14:17Z | webhook_agent.review_enforcer | Deterministic analysis missing for code diff (head=90ab4b8, ws_present=True); marking Contract Integrity, Security & Secrets, Test Coverage & Regressions UNKNOWN
  [521] 03:14:17Z | webhook_agent.review_enforcer | Deterministic Floor: Overriding LLM PASS status on 'Contract Integrity' to UNKNOWN
  [522] 03:14:17Z | webhook_agent.review_enforcer | Deterministic Floor: Overriding LLM PASS status on 'Security & Secrets' to UNKNOWN
  [523] 03:14:17Z | webhook_agent.review_enforcer | Deterministic Floor: Overriding LLM PASS status on 'Test Coverage & Regressions' to UNKNOWN
  ```
* **Code Location**: `src/webhook_agent/core/execution.py:678-714` vs `src/webhook_agent/core/prompts.py:379`
* **Mechanism**:
  - In `execution.py:678`, `deterministic_state` is assembled using:
    ```python
    "deterministic_contract_scorecard_item": event_data.get("deterministic_contract_scorecard_item"),
    "deterministic_security_scorecard_item": event_data.get("deterministic_security_scorecard_item"),
    "deterministic_test_scorecard_item": event_data.get("deterministic_test_scorecard_item"),
    ```
    At line 678, `event_data` does not have these keys yet—they are all `None`!
  - Then at line 714, `agent._build_user_message(event_data)` runs, calling `compile_pre_audit_dossier(...)` in `prompts.py:371`. It populates `event_data["deterministic_contract_scorecard_item"]`, but `deterministic_state` is **never updated**!
  - The ADK session is created with `state=deterministic_state` (where all 3 items are `None`).
  - When `_submit_formal_review` runs, `review_enforcer.py:534` checks `state_dict_val.get("deterministic_contract_scorecard_item")`. It sees `None`, assumes deterministic analysis crashed, and forcibly overrides the LLM's `PASS` scorecard to `UNKNOWN`!

---

### 🚨 Bug 3: Session ID Greedy Regex Extraction (`cgj8702-org`)
* **Log Evidence (Logs [218]–[220] & [392]–[394])**:
  ```text
  [218] 22:32:10Z | webhook_agent.processor       | Discovered session cgj8702-org from comment; inspecting state
  [219] 22:32:10Z | webhook_agent.session_api | [ERROR] Session API request failed: GET /sessions/cgj8702-org
  httpx.HTTPStatusError: Client error '404 Not Found' for url 'https://session.googleapis.com/v1alpha/sessions/cgj8702-org'
  [220] 22:32:11Z | webhook_agent.processor       | [WARNING] Could not resolve session cgj8702-org state
  ```
* **Code Location**: session ID extraction regex (lines 37-40)
* **Mechanism**:
  ```python
  _SESSION_ID_RE = re.compile(
      r"(?:https?://session\.google\.com/session/|sessions/)?"
      r"(?=[a-zA-Z0-9_-]*\d)"
      r"([a-zA-Z0-9_-]{6,})"
  )
  ```
  - The prefix `(?:...)?` is marked optional!
  - The positive lookahead `(?=[a-zA-Z0-9_-]*\d)` matches any string containing at least one digit.
  - The capture group `([a-zA-Z0-9_-]{6,})` captures any alphanumeric string >= 6 chars.
  - The repository organization name `cgj8702-org` has 11 chars and contains digits (`8702`).
  - Therefore, every PR and comment containing the repository string `cgj8702-org/hannibal-hub-agents` causes `extract_session_id()` to extract `"cgj8702-org"`, immediately firing a bogus HTTP GET request to the session API and failing with HTTP 404!

---

### 🚨 Bug 4: Issue Comments Misclassified as `formal_audit`
* **Log Evidence (Logs [59]–[78])**:
  ```text
  [059] 20:13:04Z | webhook_agent.processor     | 🚀 Webhook processing started [..., event=issue_comment.created, item_id=465]
  [070] 20:13:35Z | webhook_agent.core.guardrails | 🛑 [IterationBudgetPlugin] Turn budget of 10 exceeded on turn 11 (mode=formal_audit)
  [075] 20:14:13Z | webhook_agent.core.execution  | [WARNING] Deterministic review submission failed: 404 {"message": "Not Found", "documentation_url": "https://docs.github.com/rest/pulls/pulls#get-a-pull-request", "status": "404"}
  ```
* **Code Location**: `src/webhook_agent/core/policy.py:166-200`
* **Mechanism**:
  - Issue #465 is an Issue, not a Pull Request.
  - A comment on Issue #465 was routed into `mode=formal_audit` because prior to PR #467, review intent logic did not strictly guard on `is_pr`.
  - The agent ran for 11 turns trying to review non-existent code, burned ~70,000 tokens, hit the turn budget, and then the deterministic fallback attempted to call GitHub's Pull Request Review API (`/repos/.../pulls/465/reviews`), failing with HTTP 404!

---

### 🚨 Bug 5: Grounding Rejection Cascade & Adversarial Token Drain
* **Log Evidence (9 distinct PR review rejections)**:
  - **PR #466 (Log [106])**: `dev/rate_limit_parser.py:202` rejected (behavioral logic without test citation; exact diff quote mismatch).
  - **PR #467 (Log [137])**: `src/webhook_agent/commands.py:23` rejected (evidence text not matching diff).
  - **PR #469 (Log [205])**: `diff_filter.py:226`, `model_chain.py:108`, `prompts.py:31` rejected (3 simultaneous evidence text mismatches).
  - **PR #471 (Log [291])**: `model_chain.py:139` rejected (evidence text mismatch).
  - **PR #475 (Log [379])**: `suggestion cites 'README.md', which this PR does not change` rejected.
  - **PR #477 (Log [454])**: `schemas.py:401` evidence text mismatch.
  - **PR #478 (Logs [506], [508], [510])**: Rejection 1 (`__init__.py` deletion cited as addition), Rejection 2 (YAML line mismatch), Rejection 3 (`router/test_router.py` path prefix hallucination).
* **Code Location**: `src/webhook_agent/review/grounding.py:423-445` & `src/webhook_agent/core/prompts.py:455`
* **Mechanism**:
  - The audit prompt tells the LLM: *"Populate 2-4 verified_invariants with explicit path:line citations from the PR diff..."*
  - The model tries to comply even when PRs are pure deletions or refactors (e.g., PR #478 had -10,068 lines and only 3 lines added).
  - The model cites deleted lines or approximates text, and `grounding.py` strictly checks `touched_lines_by_file` and regex matches.
  - On rejection, the model is told to retry, passing the full 80,000 token context back to Gemini.
  - In PR #478, this triggered:
    `🛑 [GuardrailsPlugin] Tool 'review' failed 4 consecutive times. Halting execution.`

---

### 🚨 Bug 6: Strict Tool Call Budgets Blocking Benign Investigations
* **Log Evidence (Logs [078], [162], [305])**:
  ```text
  [162] 22:11:38Z | webhook_agent.core.guardrails | 🛑 [IterationBudgetPlugin] Tool call budget of 4 exceeded on call 5 for 'read_file' (mode=general_pair_programming)
  [305] 02:42:53Z | webhook_agent.processor       | tools=search_files:OK, read_file:OK, search_files:FAIL, add_comment:OK (mode=issues.typed)
  ```
* **Code Location**: `src/webhook_agent/core/guardrails.py:180-195`
* **Mechanism**:
  - `ISSUE_TRIAGE` has a strict tool budget of 2 calls. On a complex issue like #472 (`issues.typed`), the agent needed to search 2 files and read 1. The 3rd call was forcibly blocked with `search_files:FAIL`.
  - In `GENERAL_PAIR_PROGRAMMING`, a budget of 4 tool calls blocked a 5th `read_file` call, resulting in incomplete triage.

---

### 🚨 Bug 7: The Free Tier Quota Stranglehold
* **Log Evidence (20 occurrences of TPM HARD CEILING)**:
  ```text
  [468] 03:06:52Z | hannibal_rate_limiter | TPM HARD CEILING (gemini-3.5-flash-lite): Finalized 274668/250000 tokens (90% threshold). Forcing 1.7s wait...
  [473] 03:07:04Z | hannibal_rate_limiter | TPM HARD CEILING (gemini-3.5-flash-lite): Finalized 324528/250000 tokens (90% threshold). Forcing 44.7s wait...
  [494] 03:10:04Z | hannibal_rate_limiter | TPM HARD CEILING (gemini-3.5-flash-lite): Finalized 322998/250000 tokens (90% threshold). Forcing 53.9s wait...
  [498] 03:11:08Z | hannibal_rate_limiter | TPM HARD CEILING (gemini-3.5-flash-lite): Finalized 324352/250000 tokens (90% threshold). Forcing 54.0s wait...
  [504] 03:12:09Z | hannibal_rate_limiter | TPM HARD CEILING (gemini-3.5-flash-lite): Finalized 327270/250000 tokens (90% threshold). Forcing 54.5s wait...
  ```
* **Mechanism**:
  - Total LLM calls in 8 hours: 104 calls.
  - Total tokens burned: **2,984,955 tokens** (all on `gemini-3.5-flash-lite`).
  - Average prompt size: **81,000 tokens per turn**.
  - On the Free Tier, the TPM limit is 250,000.
  - Three turns in 60 seconds = 3 * 81,000 = 243,000 tokens (97% of ceiling).
  - Any review requiring more than 2 turns (or any retry) is hit with an immediate 50–55 second hard freeze.
  - The worker is still resolving `tier=free` because `WEBHOOK_TIER=paid` is not set on GCE VM instance attributes.

---

### 🚨 Bug 8: Deployment SIGTERM Restarts Interrupting Active Audits
* **Log Evidence (9 separate worker restarts)**:
  ```text
  [002] 19:38:53Z | 🛑 Signal 15 received, shutting down...
  [037] 19:50:23Z | 🛑 Signal 15 received, shutting down...
  [113] 20:30:22Z | 🛑 Signal 15 received, shutting down...
  [147] 22:07:13Z | 🛑 Signal 15 received, shutting down...
  [224] 22:33:22Z | 🛑 Signal 15 received, shutting down...
  [343] 02:47:24Z | 🛑 Signal 15 received, shutting down...
  [348] 02:48:05Z | 🛑 Signal 15 received, shutting down...
  [396] 02:55:43Z | 🛑 Signal 15 received, shutting down...
  [475] 03:07:27Z | 🛑 Signal 15 received, shutting down...
  ```
* **Mechanism**:
  - Every time a PR merges to `main`, GitHub Actions runs `.github/workflows/deploy.yml` which issues `systemctl --user restart hannibal-webhook-agent`.
  - At `03:07:27Z`, delivery `ci-gate-96b23d85` for PR #478 was actively in turn 2 when `Signal 15` killed the worker.
  - The service took 2 minutes and 13 seconds to recover (`03:07:27` to `03:09:40`).
  - Pub/Sub redelivered the PR event as delivery `ci-gate-66597f83`, forcing PR #478 to be downloaded and audited a second time from scratch!

---

## 3. Comprehensive Item-by-Item Activity Trace

| Item ID | Type | Events Received | Result / Status | Key Anomalies Detected |
| :---: | :---: | :--- | :---: | :--- |
| **#455** | Issue | `issue_comment.created`, `issues.closed` | Handled | Closed issue check invoked via `skip_closed_pr` tool. |
| **#456** | Issue | `issues.closed` | Handled | Fast skip on closed state. |
| **#457** | PR | `pull_request.synchronize` | Approved | Clean pass: 2 tools (`read_file`, `review`). |
| **#465** | Issue | `issues.opened`, `issue_comment.*`, `issues.closed` | **Failed / Aborted** | Comment triggered `mode=formal_audit` instead of triage; turn budget 10 exceeded on turn 11; attempted PR review on Issue -> 404. |
| **#466** | PR | `pull_request.opened` | Approved after retry | Grounding rejected twice on `dev/rate_limit_parser.py:202`. |
| **#467** | PR | `pull_request.opened` | Approved after retry | Grounding rejected twice on `commands.py:23`. |
| **#468** | Issue | `issues.opened`, `issues.labeled`, `issues.closed` | Handled | Owner opened issue; agent commented and closed cleanly. |
| **#469** | PR | `pull_request.opened` | Approved after retry | Grounding rejected twice on 3 file citations; 45s TPM sleep. |
| **#470** | PR | `pull_request.opened`, `pull_request.synchronize` | Approved | PR; triggered regex 404 crash on `cgj8702-org`. |
| **#471** | PR | `pull_request.opened` | Approved after retry | Grounding rejected once on `model_chain.py:139`. |
| **#472** | Issue | `issues.typed`, `issue_comment.created`, `issues.closed` | Partially Blocked | Tool budget of 2 exceeded in `issue_triage` (`search_files:FAIL`). |
| **#473** | Issue | `issues.labeled`, `issues.closed` | Handled | Summon labeled by owner; closed cleanly. |
| **#474** | PR | `pull_request.opened` | Approved after retry | Grounding rejected once on review submission. |
| **#475** | PR | `pull_request.opened` | Approved after retry | Grounding rejected on `README.md` citation (unchanged file). |
| **#476** | PR | `pull_request.opened`, `issue_comment.created` | Approved | Triggered regex 404 crash on `cgj8702-org`. |
| **#477** | PR | `pull_request.opened` | Approved after retry | Grounding rejected on `schemas.py:401`. |
| **#478** | PR | `pull_request.opened`, `pull_request.synchronize` | **Catastrophic Failure** | Killed by SIGTERM at 03:07; restarted at 03:09; failed grounding 4 times; Guardrails killswitch triggered; scorecards wiped to `UNKNOWN`; post-review zombie loop burned 400k+ tokens; manually dismissed by user with `"nah bro, chill"`. |

---

## 4. Remediation Blueprint

1. **Terminate Post-Review Loop (`execution.py:220`)**:
   - Immediately break the exploration while-loop if `any(r.tool == "review" and r.success for r in results)`. Once a review is published to GitHub, execution MUST terminate.
2. **Fix `deterministic_state` Race Condition (`execution.py:684`)**:
   - Call `_build_user_message(event_data)` *before* initializing `deterministic_state`, or backfill `session.state` with `event_data["deterministic_*_scorecard_item"]` so `review_enforcer.py` stops wiping out PASS verdicts to `UNKNOWN`.
3. **Fix Session ID Regex (lines 37-40)**:
   - Make the `session/` or `sessions/` prefix **mandatory**, or require standard UUID format `[0-9a-f-]{36}`, preventing repo strings like `cgj8702-org` from matching.
4. **Relax Deletion / Refactor Invariant Grounding (`prompts.py:455`)**:
   - Explicitly instruct the model: *"For pure deletion or refactoring PRs with fewer than 2 added lines in code files, do not invent verified invariants; return empty `verified_invariants: []` and rely on `semantic_invariants`."*
5. **Activate Paid Tier on GCE VM**:
   - Add instance attribute `WEBHOOK_TIER=paid` via `gcloud compute instances add-metadata hannibal-hub-free --metadata=WEBHOOK_TIER=paid` to immediately unlock 4,000,000 TPM and stop all 54-second sleep stalls.
