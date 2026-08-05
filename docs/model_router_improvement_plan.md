# Dynamic Model Router Improvement Plan

> Comparing `hannibal-hub-agents` event-type-based model routing vs `adk-samples` patterns

---

## Executive Summary

Our dynamic model router (`_select_model_for_event()` in `webhook_agent.py`) classifies incoming webhook events by complexity and routes them to different models: heavy events (PR opens, slash commands, @mentions) go to the primary model (`GEMMA_MODEL`), while routine events (closes, casual comments, label changes) go to a lightweight model (`GEMMA_LIGHTWEIGHT_MODEL`). This saves quota and reduces latency for simple events.

Google's `adk-samples` repository takes two different approaches. The **Software Bug Assistant** uses a single hardcoded model without routing. The **Long Horizon Harness (LHA)** uses a `select_model_callback` (`before_model_callback`) that resolves the model per-turn via a precedence chain, combined with a `DispatchingLlm` wrapper that routes per-call to the right backend without recreating the Runner or hot-swapping `agent.model`.

### 🚫 Architectural Decision: Exclusion of Persistent Session State Model Overrides

While LHA uses `session.state["selected_model"]` for session-wide overrides, **we explicitly exclude persistent session state model selection from our architecture.**

In a webhook-driven agent framework handling multi-turn GitHub issue/PR sessions:
- Webhook events arrive asynchronously for the same issue or PR (sharing a single session ID).
- If a heavy command (e.g., `/review`) sets `session.state["selected_model"] = "gemini-3.6-flash"`, subsequent routine lifecycle events on the same issue (such as adding a label, closing the issue, or leaving a casual comment) would incorrectly inherit the expensive model tier indefinitely.
- Conversely, if a routine event sets a lightweight model in session state, a subsequent heavy command in the same thread would be trapped using an under-powered model.

Therefore, **model routing in `hannibal-hub-agents` must remain strictly stateless and evaluated on a per-event/per-turn basis.**

### 🎯 Proposed Improvement

By combining our event-type classification logic with LHA's `before_model_callback` and `DispatchingLlm` wrapper—while keeping session state stateless regarding model selection—we achieve:
1. **Stateless, Per-Event Default Routing**: Dynamic tier selection based on webhook payload payload signals without session state bleed.
2. **Clean Abstraction (`DispatchingLlm`)**: Eliminates dangerous runtime hot-swapping of `agent.model`.
3. **Resilient Rate Limiting & Fallbacks**: Integrates seamlessly with our existing TPM sliding window and fallback cascades.

---

## Table of Contents

1. [Current Implementation: Event-Type-Based Router](#1-current-implementation-event-type-based-router)
2. [adk-samples Reference Implementations](#2-adk-samples-reference-implementations)
3. [Side-by-Side Comparison](#3-side-by-side-comparison)
4. [Issues in Current Implementation](#4-issues-in-current-implementation)
5. [Improvement Plan](#5-improvement-plan)
6. [Migration Strategy](#6-migration-strategy)
7. [Risk Assessment](#7-risk-assessment)

---

## 1. Current Implementation: Event-Type-Based Router

### Architecture

Our current router is the `_select_model_for_event()` function in `webhook_agent.py`. It classifies each webhook event as "heavy" or "routine" and selects the appropriate model tier. The selected model is then hot-swapped onto the live Agent before the ADK Runner processes the event.

```
GitHub Webhook (Pub/Sub)
    │
    ▼
WebhookAgent.plan_and_execute()
    │
    ├─ Policy gates (bot/read-only/mutation/dry-run)
    ├─ Session derivation (repo+issue# for continuity)
    ├─ User message construction (token-capped)
    │
    ├─ DYNAMIC MODEL ROUTING  ← _select_model_for_event()
    │   ├─ Classify event type + content
    │   ├─ Heavy? → primary model (GEMMA_MODEL, default gemini-3.6-flash)
    │   ├─ Routine? → lightweight model (GEMMA_LIGHTWEIGHT_MODEL, default gemini-3.5-flash-lite)
    │   └─ Hot-swap: self._agent.model = Gemini(model=selected_model)
    │
    ├─ PROACTIVE: TPM pacing (Gemma-only, sliding 60s window)
    │
    └─ REACTIVE: Retry/fallback loop (TPM-descending cascade on 429/503)
         │
         for attempt in range(_MAX_RETRIES):
             ├─ runner.run_async(session, new_message)
             └─ on transient error → _advance_model_chain() → retry
```

### Routing Logic

```python
def _select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type and content commands.

    Routes heavy workloads (pull_request.opened, slash commands, @mentions)
    to the primary model (GEMMA_MODEL), and routine lifecycle events
    (closed, reopened, labels, casual comments) to the lightweight model
    (GEMMA_LIGHTWEIGHT_MODEL).
    """
    primary = os.environ.get("GEMMA_MODEL", "gemini-3.6-flash")
    lightweight = os.environ.get("GEMMA_LIGHTWEIGHT_MODEL", "gemini-3.5-flash-lite")

    # Kill switch — when disabled, all events go to primary
    if os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", "1") not in (
        "1", "true", "True",
    ):
        return primary

    canonical = event_data.get("canonical", "")
    raw = event_data.get("raw_payload", {})

    # Heavy: PR opened → needs full code review capability
    if canonical == "pull_request.opened":
        return primary

    # Heavy: Comments with slash commands or bot mentions
    if canonical.startswith("issue_comment.") or canonical.startswith(
        "pull_request_review_comment."
    ):
        comment_body = ""
        if isinstance(raw.get("comment"), dict):
            comment_body = raw["comment"].get("body") or ""
        if any(cmd in comment_body for cmd in (
            "/review", "/create", "/resolve", "/help", "@hannibal-hub-agents"
        )):
            return primary

    # Routine: everything else (closes, reopens, labels, casual comments)
    return lightweight
```

### Call Site

```python
# Select model tier dynamically for this event
selected_model = _select_model_for_event(event_data)
if self._current_model_name != selected_model:
    logger.info(
        "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
        selected_model,
        canonical,
        trace_id[-4:],
    )
    self._current_model_name = selected_model
    self._agent.model = Gemini(model=selected_model)  # Hot-swap
```

### Event Classification Summary

| Event Type | Classification | Model Tier | Rationale |
|------------|---------------|------------|-----------|
| `pull_request.opened` | Heavy | `GEMMA_MODEL` | Deep diff analysis & code review |
| `issue_comment.created` (`/review`, `/create`, `/resolve`) | Heavy | `GEMMA_MODEL` | Explicit action commands |
| `issue_comment.created` (`@hannibal-hub-agents`) | Heavy | `GEMMA_MODEL` | Direct conversational request |
| `issue_comment.created` (casual) | Routine | `GEMMA_LIGHTWEIGHT_MODEL` | Simple response / ack |
| `pull_request.closed` / `reopened` | Routine | `GEMMA_LIGHTWEIGHT_MODEL` | Status update / lifecycle tracking |
| `label.added` / `removed` | Routine | `GEMMA_LIGHTWEIGHT_MODEL` | Metadata tracking |

---

## 2. adk-samples Reference Implementations

### 2.1 Software Bug Assistant Pattern
The Software Bug Assistant sample utilizes a single, static model defined at initialization. It has no routing or dynamic fallback capabilities.

### 2.2 Long Horizon Harness (LHA) Pattern
The LHA pattern introduces dynamic model routing using two primary components:
1. **`before_model_callback` / `select_model_callback`**: Callback invoked prior to LLM execution to evaluate which model to invoke.
2. **`DispatchingLlm` Wrapper**: A wrapper class implementing the model interface that routes requests to underlying model backends without mutating the agent object or recreating the runner.

In LHA, `session.state["selected_model"]` is used to persist model overrides across turns. **However, as noted in our architectural decision, persistent session state overrides are ill-suited for GitHub event webhooks where event complexity fluctuates between heavy commands and routine updates within the same conversation session.**

---

## 3. Side-by-Side Comparison

| Architectural Feature | `hannibal-hub-agents` Current | `adk-samples` (LHA) | Proposed Refined Architecture |
|-----------------------|--------------------------------|---------------------|-------------------------------|
| **Routing Strategy** | Event-type payload classification | Per-turn callback (`select_model_callback`) | **Stateless Per-Event Callback** |
| **State Persistence** | Stateless | Persistent (`session.state["selected_model"]`) | **Strictly Stateless (No Session State Overrides)** |
| **Model Swapping** | Direct mutation (`agent.model = ...`) | `DispatchingLlm` multi-backend wrapper | **`DispatchingLlm` Wrapper** |
| **Error Fallbacks** | Cascading TPM retry loop | Environment variable precedence chain | **Cascading TPM Retry Loop + Fallback Tier** |
| **Concurrency Safety**| Vulnerable to race conditions | Thread-safe via wrapper dispatch | **Thread-safe & Purely Functional** |

---

## 4. Issues in Current Implementation

1. **Mutable Agent Hot-Swapping**:
   Mutating `self._agent.model = Gemini(model=selected_model)` directly updates global/shared agent state. If events are processed concurrently or in multi-threaded contexts, mutating `self._agent.model` introduces race conditions.

2. **Coupling Runner to Agent Instance**:
   Changing models via object attribute replacement requires manual state tracking (`self._current_model_name`) and bypasses ADK's standard callback lifecycle.

3. **Potential Session State Contamination (if persistent state were introduced)**:
   If model selection were stored in `session.state`, a user running `/review` on PR #10 would cause subsequent `label` or `closed` events on PR #10 to incorrectly run on the heavy model tier.

---

## 5. Improvement Plan

### Phase 1: Implement `DispatchingLlm` Pattern
Replace direct `self._agent.model` assignment with a `DispatchingLlm` class that wraps model backends:
- Maintain pre-initialized instances for `GEMMA_MODEL` and `GEMMA_LIGHTWEIGHT_MODEL`.
- Dispatch model invocations based on context without modifying the `Agent` instance.

### Phase 2: Refactor to Stateless `before_model_callback`
- Implement `before_model_callback` in ADK to select the active model key on a per-turn basis.
- Pass event metadata (event type, comment commands) via request execution context rather than mutating `session.state`.
- Explicitly avoid writing or reading `session.state["selected_model"]`.

### Phase 3: Unified TPM Pacing & Fallback Cascade
- Standardize error handling (HTTP 429/503) so that if a primary model is rate-limited, the `DispatchingLlm` falls back to the lightweight tier or fallback chain cleanly within the same request lifecycle.

### Phase 4: Model Capability Metadata Integration
- Define model metadata (max tokens, tool schema formatting requirements, cost factor) in a central registry to ensure dynamic switching respects model capability boundaries.

---

## 6. Migration Strategy

1. **Step 1 (Infrastructure)**: Introduce `DispatchingLlm` in `src/webhook_agent/agent_core.py`.
2. **Step 2 (Callback Routing)**: Migrate `_select_model_for_event()` into a stateless context provider for `before_model_callback`.
3. **Step 3 (Cleanup)**: Remove `self._agent.model = Gemini(...)` hot-swap calls in `webhook_agent.py`.
4. **Step 4 (Validation)**: Update `test_agent_core.py` to verify that multi-turn sessions with mixed event types (e.g., `/review` followed by casual comment) evaluate models independently without state bleed.

---

## 7. Risk Assessment

| Risk | Likelihood | Impact | Mitigation Strategy |
|------|------------|--------|---------------------|
| **Session State Bleed** | High (if state used) | Medium | **Eliminated by strictly keeping routing stateless and omitting `session.state["selected_model"]`.** |
| **Race Conditions in Multi-Threading** | Low | High | Resolved by adopting thread-safe `DispatchingLlm` wrapper. |
| **Rate Limit Cascades** | Medium | Low | Preserved existing sliding window TPM rate-limiter and fallback cascade. |
| **Callback Overhead** | Low | Low | Evaluation involves fast in-memory payload inspection. |
