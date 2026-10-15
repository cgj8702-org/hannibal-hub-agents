# Hannibal Hub Agents — Full Codebase Audit & Step-by-Step Implementation Plan

## 1. Executive Summary & Target Architecture

By retiring CodeRabbit AI and promoting `hannibal-hub-agents` to your sole autonomous PR reviewer, you eliminate the `jules_proxy.py` PR-duplication workaround, fix five critical P0 bugs discovered in the codebase audit, and upgrade your review engine with right-side line-numbered diffs (`pr-agent` pattern) and linked-issue task specification verification (`ocr` pattern).

```text
[systemd: hannibal-webhook-agent.service]
   └── worker.py (single-process entry point + Pub/Sub lease extender)
         └── WebhookProcessor.process_event(payload)
               ├── Stage 1: Ingress & Strict Bot Identity Gate (bot_identity.py)
               │     ├── Suppress own bot events (`is_hannibal_bot_login`)
               │     ├── Allow `google-labs-jules[bot]` PR lifecycle events natively (no proxy!)
               │     ├── Suppress third-party bot comments (dependabot, github-actions, etc.)
               │     └── Live PR state & closed-PR short-circuit check
               ├── Stage 2: Deterministic Pre-Flight Gates
               │     ├── Base-branch merge sync skip (`is_base_branch_merge_sync`)
               │     ├── Stateless CI Gate (`ci_gate.py`: hold auto-review until CI is 100% green)
               │     └── Dependabot lockfile validator fast-path (0-token instant APPROVE)
               ├── Stage 3: Single-Pass PR Context Hydration (pr_context.py)
               │     ├── PR metadata & filtered diff with right-side line numbers (`pr-agent` pattern)
               │     ├── Linked Issue / Task Spec extraction (`Fixes #N` -> `ocr` spec check)
               │     └── Prior reviews filtered STRICTLY to `is_hannibal_bot_login()`
               ├── Stage 4: Diff-First Static Analysis (No Stale VM Disk Reads)
               │     ├── Diff-first AST & structural defect analyzer (`verify_python_ast`)
               │     └── Diff-aware symbol & test impact analysis
               ├── Stage 5: ADK Runner Execution (1-Turn JSON Audit or Conversational Peer)
               │     ├── Single async token metering + sliding-window RPM/TPM limiter
               │     └── Registered plugins: HistoryPruning, ToolOutputPruning, PromptSanitizer
               └── Stage 6: Unified Review Verification, Round Decay, & Writeback
                     ├── Citation grounding (`grounding.py`) & round-decay budget (`review_budget.py`)
                     └── Idempotent GitHub review submission (dismisses ONLY prior Hannibal reviews)
```

---

## 2. Pre-Flight Checklist (Before Starting Code Changes)

1. **Uninstall / Suspend CodeRabbit AI in GitHub:**
   * Go to `https://github.com/organizations/cgj8702-org/settings/installations` $\to$ **CodeRabbit** $\to$ **Suspend** or **Uninstall**.
2. **Verify Local Clone & Pre-Commit Hook on VM:**
   ```bash
   cd /home/carly/hannibal-hub-agents
   git checkout main && git pull origin main
   ./scripts/install-git-hooks.sh
   uv sync
   ```
3. **Ensure Commit Author Identity (Per `AGENTS.md` Section 4):**
   ```bash
   git config user.name "cgj8702-agents"
   git config user.email "cgj8702-agents@users.noreply.github.com"
   ```

---

## 3. Phase 1 (PR #1): Cut Over from CodeRabbit, Retire `jules_proxy`, & Fix Bot Identity Collisions

**Branch:** `git checkout -b feat/native-hannibal-reviews`

### Step 1.1: Update Operational Defaults in `src/webhook_agent/constants.py`
Enable automatic CI-gated PR reviews (`DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS = "1"`) and disable the legacy Jules proxy (`DEFAULT_ENABLE_JULES_PR_PROXY = "0"`).

```python
# In src/webhook_agent/constants.py — replace the "--- Operational Policy Defaults ---" block (lines 25-39):

# --- Operational Policy Defaults ---
# Fail open / allowed by default: automated mutations enabled out of the box.
DEFAULT_ALLOW_AUTOMATED_MUTATIONS = "1"
# Fail closed: citation grounding is measured (logged) first and only enforced once its
# false-positive rate is known. See docs/plans/reviewer_hardening_plan.md Phase 2.2.
DEFAULT_STRICT_REVIEW_GROUNDING = "0"
# Hold automatic reviews until the PR head commit has fully green CI (silent skip on failure).
# Set REVIEW_WAIT_FOR_CI=0 to restore review-immediately behaviour. See review/ci_gate.py.
DEFAULT_REVIEW_WAIT_FOR_CI = "1"
# Primary autonomous PR reviewer: automatically review opened/synchronized PRs once CI is green.
# Set ENABLE_AUTOMATIC_PR_REVIEWS=0 to require manual /review triggers.
DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS = "1"
# Legacy CodeRabbit workaround proxy disabled by default; Hannibal reviews Jules PRs natively.
DEFAULT_ENABLE_JULES_PR_PROXY = "0"
DEFAULT_WEBHOOK_TIER = "free"
```

### Step 1.2: Add `is_hannibal_bot_login()` to `src/webhook_agent/github/bot_identity.py`
Fix **P0-1** by adding a strict self-identity check so third-party `[bot]` reviews (e.g., historical `coderabbitai[bot]` or `github-actions[bot]`) are never mistaken for Hannibal's own reviews, while suppressing third-party bot comments.

```python
# In src/webhook_agent/github/bot_identity.py — add is_hannibal_bot_login() and update _is_bot_sender():

def is_hannibal_bot_login(login: str | None) -> bool:
    """Return True strictly when login belongs to this GitHub App (hannibal-hub-agents)."""
    norm = (login or "").strip().lower()
    return norm in {
        BOT_LOGIN.lower(),
        BOT_APP_SLUG.lower(),
        "hannibal-hub-agents[bot]",
        "hannibal-hub-agents",
    }


def _is_bot_sender(sender: dict[str, Any] | None) -> bool:
    """Check whether a sender dict represents this app's bot identity or a suppressed bot.

    Uses multiple signals to avoid false-negatives when GitHub varies
    the sender format across event types or API versions.
    """
    if not isinstance(sender, dict):
        return False
    login = (sender.get("login") or "").strip().lower()
    sender_type = (sender.get("type") or "").strip()

    # Jules is an external mutation partner bot; its PR lifecycle events are reviewed natively
    if is_jules_sender(sender):
        return False

    if is_hannibal_bot_login(login):
        return True

    suppressed_bot_logins = {
        "github-actions[bot]",
        "coderabbitai[bot]",
        "coderabbitai",
    }
    if login in suppressed_bot_logins:
        return True

    if login.endswith("[bot]") and ("hannibal" in login or "agent" in login):
        return True

    return bool(sender_type == "Bot" and ("hannibal" in login or "agent" in login))
```
Also export `is_hannibal_bot_login` in `src/webhook_agent/github/__init__.py` and `src/webhook_agent/bot_identity.py`.

### Step 1.3: Gate `handle_jules_pr_opened()` in `src/webhook_agent/github/jules_proxy.py`
Gate `handle_jules_pr_opened()` behind `ENABLE_JULES_PR_PROXY` (defaulting to `"0"`) and remove the `@coderabbitai full review` comment on line 246 so Jules PRs remain native.

```python
# In src/webhook_agent/github/jules_proxy.py — update handle_jules_pr_opened() (lines 253-270):
from webhook_agent.constants import DEFAULT_ENABLE_JULES_PR_PROXY


def is_jules_pr_proxy_enabled() -> bool:
    """Return True only when ENABLE_JULES_PR_PROXY is explicitly enabled."""
    val = os.environ.get("ENABLE_JULES_PR_PROXY", DEFAULT_ENABLE_JULES_PR_PROXY)
    return val.strip().lower() in ("1", "true", "yes", "on")


def handle_jules_pr_opened(
    gh_app: Github,
    repo_name: str,
    payload: dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """Detect and process PR opened events authored by Jules when proxy mode is enabled."""
    if not is_jules_pr_proxy_enabled():
        return False
    # ... existing implementation below (with `new_pr_obj.create_issue_comment("@coderabbitai full review")` removed) ...
```

### Step 1.4: Fix Bot Review Filtering & Deduplicate Prefetch in `src/webhook_agent/github/pr_context.py` and `src/webhook_agent/processor.py`
1. In `src/webhook_agent/github/pr_context.py`:
   * Import `is_hannibal_bot_login` from `webhook_agent.github.bot_identity`.
   * On line 169 inside `_prefetch_previous_bot_reviews()`, replace:
     ```python
     if "hannibal-hub-agents" in login or login.endswith("[bot]"):
     ```
     with:
     ```python
     if is_hannibal_bot_login(login):
     ```
   * Add `_prefetch_linked_issue_spec(gh, repo_name, payload)` (`ocr` pattern) and call it at the end of `_prefetch_pr_diff()`:
     ```python
     _LINKED_ISSUE_RE = re.compile(
         r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
         re.IGNORECASE,
     )


     def _prefetch_linked_issue_spec(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
         """Pre-fetch linked GitHub Issue body when PR description references 'Fixes #N' / 'Closes #N'."""
         try:
             raw = payload.get("raw_payload")
             if not isinstance(raw, dict) or "linked_issue_spec" in raw:
                 return
             pr_data = raw.get("pull_request")
             if not isinstance(pr_data, dict):
                 return
             pr_body = str(pr_data.get("body") or "")
             match = _LINKED_ISSUE_RE.search(pr_body)
             if not match:
                 return
             issue_number = int(match.group(1))
             repo = gh.get_repo(repo_name)
             issue = repo.get_issue(issue_number)
             if getattr(issue, "pull_request", None) is not None:
                 return
             issue_title = getattr(issue, "title", "") or ""
             issue_body = (getattr(issue, "body", "") or "").strip()
             if issue_title or issue_body:
                 raw["linked_issue_spec"] = (
                     f"Linked Issue #{issue_number}: {issue_title}\n\n{issue_body}"
                 )
                 logger.info(
                     "Pre-fetched linked issue #%d spec for PR review grounding", issue_number
                 )
         except Exception as exc:
             logger.debug("Could not pre-fetch linked issue spec: %s", exc)
     ```
2. In `src/webhook_agent/review/ci_gate.py`:
   * Import `is_hannibal_bot_login` from `webhook_agent.github.bot_identity`.
   * In `last_bot_review_sha()` (line 138), replace `if "hannibal-hub-agents" not in login:` with `if not is_hannibal_bot_login(login):`.

### Step 1.5: Fix Bot Review Filtering & Wire Round-Decay Budget in `src/webhook_agent/review/review_enforcer.py`
Fix **P0-1** and **P0-3** in `src/webhook_agent/review/review_enforcer.py`:
1. Import `is_hannibal_bot_login` from `webhook_agent.github.bot_identity`.
2. Replace both occurrences of `if "hannibal-hub-agents" in login or login.endswith("[bot]"):` (lines 100 and 294) with `if is_hannibal_bot_login(login):`.
3. In `_enforce_verdict()` (lines 118–132), wire `summarise_review_history()` and `compute_round_allowance()` so comment budgets decay across sync rounds ($5 \to 3 \to 1$):

```python
        try:
            bot_review_comments: list[Any] = []
            for c in pr.get_review_comments():
                c_user = getattr(c, "user", None)
                c_login = (getattr(c_user, "login", "") or "").lower() if c_user else ""
                if is_hannibal_bot_login(c_login):
                    bot_review_comments.append(c)
                existing_comments_data.append(
                    {
                        "path": getattr(c, "path", ""),
                        "line": getattr(c, "line", None) or getattr(c, "original_line", None),
                        "body": getattr(c, "body", ""),
                    }
                )
            if review_budget is not None and bot_reviews:
                from webhook_agent.models.review_budget import (
                    compute_round_allowance,
                    summarise_review_history,
                )

                head_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")
                hist = summarise_review_history(bot_reviews, bot_review_comments, head_sha=head_sha)
                review_budget = compute_round_allowance(
                    round_number=hist.round_number,
                    previous_round_posted=hist.previous_round_posted,
                    total_posted_so_far=hist.total_posted_so_far,
                    initial_budget=review_budget,
                )
        except Exception as comm_err:
            logger.debug("Could not fetch existing PR review comments: %s", comm_err)
```

### Step 1.6: Update Unit Tests & Verify PR #1
1. Update `tests/unit/test_constants_and_secrets.py` and `tests/unit/test_worker.py` to expect `DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS == "1"`.
2. Add a test in `tests/unit/github/test_bot_identity_and_jules.py` verifying `is_hannibal_bot_login("coderabbitai[bot]") is False` and `is_hannibal_bot_login("hannibal-hub-agents[bot]") is True`.
3. Run the verification gate:
   ```bash
   ./scripts/ruff-all.sh
   uv run pytest tests/unit/github/test_bot_identity_and_jules.py tests/unit/github/test_pr_context.py
   uv run pytest --tb=short -q
   ```

---

## 4. Phase 2 (PR #2): Diff-First Static Analysis, Core Security Wiring, & Retry Loop Fix

**Branch:** `git checkout -b fix/diff-first-ast-and-agent-wiring`

### Step 2.1: Fix Local VM Disk Bleed in `src/webhook_agent/tools/ast_tools.py` (P0-2)
Currently, `verify_python_ast(file_path, code_snippet, diff_text)` checks `Path(file_path).is_file()` on the **local VM disk first** (`/home/carly/hannibal-hub-agents` checked out on `main`) before checking `diff_text`. Flip the priority so `diff_text` always takes precedence over disk when provided:

```python
# In src/webhook_agent/tools/ast_tools.py — replace lines 85-114 inside verify_python_ast():
    code_text = code_snippet
    if not code_text or not code_text.strip():
        cleaned_path = _strip_diff_prefix(file_path)
        if diff_text:
            try:
                from webhook_agent.tools.diff_tools import walk_right_side

                _, text_map = walk_right_side(diff_text)
                file_lines = text_map.get(cleaned_path) or text_map.get(file_path)
                if file_lines:
                    sorted_lines = [file_lines[k] for k in sorted(file_lines.keys())]
                    code_text = "\n".join(sorted_lines)
            except Exception as diff_err:
                logger.debug(
                    "Could not extract lines from diff_text for %s: %s", file_path, diff_err
                )

        if not code_text or not code_text.strip():
            candidates = [Path(file_path), Path(cleaned_path)]
            target_file = next((p for p in candidates if p.is_file()), None)
            if target_file:
                try:
                    code_text = target_file.read_text(encoding="utf-8", errors="replace")
                except Exception as read_err:
                    return f"Error reading file '{file_path}': {read_err}"

        if not code_text or not code_text.strip():
            return (
                f"Error: No code snippet provided and file '{file_path}' was not found on disk. "
                "Please pass code_snippet directly to verify_python_ast."
            )
```

### Step 2.2: Fix `PromptSanitizerPlugin` in `src/webhook_agent/core/sanitizer_plugin.py` (P0-4)
Update `PromptSanitizerPlugin.after_model_callback` so it sanitizes both `google.genai.types.Content` (`.parts[*].text`) and string content, and accepts keyword arguments (`*, callback_context, llm_response`) matching ADK's `BasePlugin` contract:

```python
# In src/webhook_agent/core/sanitizer_plugin.py — replace PromptSanitizerPlugin (lines 43-58):
class PromptSanitizerPlugin(BasePlugin):
    """ADK Plugin intercepting model responses to strip prompt leakage and secrets."""

    def __init__(self, name: str = "prompt_sanitizer_plugin") -> None:
        super().__init__(name=name)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Intercept and sanitize model output before rendering or downstream processing."""
        if hasattr(llm_response, "content") and llm_response.content:
            if isinstance(llm_response.content, str):
                llm_response.content = sanitize_markdown_text(llm_response.content)
            else:
                parts = getattr(llm_response.content, "parts", None) or []
                for part in parts:
                    if hasattr(part, "text") and isinstance(part.text, str) and part.text:
                        part.text = sanitize_markdown_text(part.text)

        return None
```

### Step 2.3: Wire `create_issue` & `PromptSanitizerPlugin` in `src/webhook_agent/core/agent_definition.py` (P0-4)
1. Import `create_issue` alongside `get_issue, read_file, review` from `webhook_agent.tools.github_tools`.
2. Store `self._sanitizer_plugin = PromptSanitizerPlugin()` instead of discarding the return value on line 141.
3. Add `create_issue` to `self._conversational_agent` tools (`tools=[read_file, get_issue, create_issue]`).
4. Register `self._sanitizer_plugin` in `plugins=[self._history_pruning_plugin, self._tool_pruning_plugin, self._sanitizer_plugin]` for both `self._app` and `self._conversational_app`.

### Step 2.4: Fix In-Place Prompt Mutation Across Retries in `src/webhook_agent/core/execution.py` (P0-5)
In `src/webhook_agent/core/execution.py`, capture `base_prompt_text` before `for attempt in range(_MAX_RETRIES):` so retry attempts do not cumulatively append duplicate `[⚠️ SYSTEM NOTICE]` and `[YOUR PREVIOUS REVIEW CRITIQUE]` blocks:

```python
# In src/webhook_agent/core/execution.py — inside _run() right before `for attempt in range(_MAX_RETRIES):`:
        base_prompt_text = (
            getattr(user_message.parts[0], "text", "") or ""
            if user_message.parts
            else ""
        )

        for attempt in range(_MAX_RETRIES):
            try:
                if user_message.parts and hasattr(user_message.parts[0], "text"):
                    user_message.parts[0].text = base_prompt_text
```

### Step 2.5: Inject Linked Issue Spec & Right-Side Line Numbers in `src/webhook_agent/core/prompts.py`
In `build_user_message()` in `src/webhook_agent/core/prompts.py`:
1. Right after `if "commit_diff" in raw:`, inject `linked_issue_spec` if pre-fetched:
   ```python
   if "linked_issue_spec" in raw and raw["linked_issue_spec"]:
       parts.append(
           f"\n### 🎯 Linked Issue / Task Specification (Verify Acceptance Criteria)\n"
           f"{raw['linked_issue_spec']}\n"
           f"Verify that the PR implementation fulfills all requirements and constraints in this linked issue."
       )
   ```
2. Guard `SymbolImpactAnalyzer` and `TestImpactAnalyzer` so they only run when reviewing `hannibal-hub-agents` itself (preventing cross-repo file collisions when reviewing `cgj8702-org/hannibal-hub`):
   ```python
   repo_full_name = (event_data.get("repository") or {}).get("full_name", "")
   is_self_repo = repo_full_name.endswith("/hannibal-hub-agents") or not repo_full_name
   ```

### Step 2.6: Verify PR #2
```bash
./scripts/ruff-all.sh
uv run pytest tests/unit/tools/test_ast_tools.py tests/unit/core/test_execution.py tests/unit/core/test_prompts.py
uv run pytest --tb=short -q
```

---

## 5. Phase 3 (PR #3): Single-Pass Token Metering, Pub/Sub Lease Heartbeat, & Single-Process Worker

**Branch:** `git checkout -b perf/single-token-metering-and-pubsub-lease`

### Step 3.1: Eliminate Duplicate Synchronous `count_tokens` in `src/webhook_agent/core/callbacks.py`
Currently, `before_model_callback` creates a brand-new `genai.Client(api_key=api_key)` and makes a synchronous `client.models.count_tokens()` call on every turn, and then `RateLimitedGemini.generate_content_async()` in `model_factory.py` makes a *second* `count_tokens` call milliseconds later.
* **Change:** In `src/webhook_agent/core/callbacks.py`, use the shared client via `count_tokens_exact` or let `before_model_callback` use `get_shared_genai_client()` instead of instantiating `genai.Client(api_key=api_key)` on every request, and skip the second `count_tokens` call inside `RateLimitedGemini.generate_content_async()` when `getattr(llm_request, "_rate_limit_checked", False)` is already `True`:

```python
# In src/webhook_agent/models/model_factory.py — inside RateLimitedGemini.generate_content_async():
        if not getattr(llm_request, "_rate_limit_checked", False):
            estimated_tokens = 0
            try:
                if self.api_client:
                    ct_resp = await self.api_client.aio.models.count_tokens(
                        model=model_name,
                        contents=llm_request.contents,
                    )
                    if ct_resp and ct_resp.total_tokens:
                        estimated_tokens = int(ct_resp.total_tokens)
            except Exception as exc:
                logger.debug(
                    "Free count_tokens API call skipped on model '%s': %s", model_name, exc
                )

            if estimated_tokens <= 0:
                contents_str = str(getattr(llm_request, "contents", ""))
                estimated_tokens = max(1, len(contents_str) // 4)

            try:
                await rpm_waiter.check_and_wait(
                    model=model_name,
                    estimated_tokens=estimated_tokens,
                    tier=active_tier,
                )
            except Exception as exc:
                logger.warning(
                    "RPM/TPM pre-flight check error on model '%s' (tier '%s'): %s",
                    model_name,
                    active_tier,
                    exc,
                )
```

### Step 3.2: Add Pub/Sub Lease Extension Heartbeat in `src/webhook_agent/worker.py`
When `processor.process_event(payload)` waits on rate-limiter sleeps or model failovers ($15\text{–}90\text{s}$), the default Pub/Sub ack deadline can expire, causing Pub/Sub to redeliver the message while it's still running. Add a lightweight daemon heartbeat context manager in `worker.py`:

```python
# Add to src/webhook_agent/worker.py:
import contextlib
import threading


@contextlib.contextmanager
def _lease_extender(
    subscriber: pubsub_v1.SubscriberClient,
    subscription_path: str,
    ack_id: str,
    ack_deadline_seconds: int = 60,
    interval_seconds: float = 20.0,
) -> Any:
    """Periodically extend the Pub/Sub message ack deadline while processing is in flight."""
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                subscriber.modify_ack_deadline(
                    request={
                        "subscription": subscription_path,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": ack_deadline_seconds,
                    }
                )
            except Exception as exc:
                logger.debug("Pub/Sub lease extension heartbeat skipped: %s", exc)

    thread = threading.Thread(target=_heartbeat, name="PubSubLeaseExtender", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
```
Then wrap `processor.process_event(payload)` in `worker.py` with `with _lease_extender(subscriber, subscription_path, ack_id):`.

### Step 3.3: Verify PR #3
```bash
./scripts/ruff-all.sh
uv run pytest tests/unit/core/test_callbacks.py tests/unit/models/test_model_factory.py tests/unit/test_worker.py
uv run pytest --tb=short -q
```

---

## 6. Phase 4 (PR #4): Remove Legacy Shims, Duplicate Classes, & Binary PDF Bloat

**Branch:** `git checkout -b chore/remove-legacy-shims-and-dead-code`

### Step 4.1: Update Any Remaining Shim Imports in `src/` and `tests/`
Run a quick search for any imports still referencing the 11 transitional shim modules:
```bash
grep -rnE "from webhook_agent\.(bot_identity|callbacks|cancellation|fast_path|formatter|github_credential_helper|memory_service|pr_context|proactive_service|sanitizer_plugin|schemas) import" src/ tests/
grep -rn "from webhook_agent.core.github_tools import" src/ tests/
```
Update any matches to their canonical subpackage paths (`webhook_agent.github.*`, `webhook_agent.core.*`, `webhook_agent.review.*`, `webhook_agent.tools.github_tools`).

### Step 4.2: `git rm` the 11 Transitional Shim Modules
```bash
git rm \
  src/webhook_agent/bot_identity.py \
  src/webhook_agent/callbacks.py \
  src/webhook_agent/cancellation.py \
  src/webhook_agent/fast_path.py \
  src/webhook_agent/formatter.py \
  src/webhook_agent/github_credential_helper.py \
  src/webhook_agent/memory_service.py \
  src/webhook_agent/pr_context.py \
  src/webhook_agent/proactive_service.py \
  src/webhook_agent/sanitizer_plugin.py \
  src/webhook_agent/core/github_tools.py
```

### Step 4.3: Consolidate `DepletedModelRegistry` in `src/webhook_agent/models/model_chain.py`
Remove the duplicate 105-line `DepletedModelRegistry` class definition from `src/webhook_agent/models/model_chain.py` (lines 25–129) and import `FirestoreDepletedModelRegistry as DepletedModelRegistry` and `firestore_depleted_registry as _DEPLETED_MODEL_REGISTRY` directly from `webhook_agent.models.depleted_registry`.

### Step 4.4: Verify Full Suite & Deploy
```bash
./scripts/ruff-all.sh
uv run pytest --tb=short -q
```