# Dynamic Model Router Improvement Plan

> Comparing `hannibal-hub-agents` event-type-based model routing vs `adk-samples` patterns and proposing a Hybrid Model Routing Architecture.

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

### 💡 The Hybrid Model Routing Approach

To combine the strengths of both approaches, we propose a **Hybrid Model Routing Architecture**:
1. **Layer 1: Fast Webhook Payload Heuristics**: Immediate pre-classification based on event payload type, action, slash commands (`/review`, `/create`, `/resolve`), and `@mentions`.
2. **Layer 2: ADK `before_model_callback` Context Evaluator**: Per-turn inspection of prompt token size, conversation history depth, and explicit inline command overrides (e.g. `model=pro` or `/review --tier=heavy`).
3. **DispatchingLLM Wrapper Execution**: Seamless routing to model backends without mutating `agent.model` or polluting persistent session state.

---

## Table of Contents

1. [Current Implementation: Event-Type-Based Router](#1-current-implementation-event-type-based-router)
2. [adk-samples Reference Implementations](#2-adk-samples-reference-implementations)
3. [Side-by-Side Comparison](#3-side-by-side-comparison)
4. [Issues in Current Implementation](#4-issues-in-current-implementation)
5. [The Hybrid Model Routing Architecture](#5-the-hybrid-model-routing-architecture)
6. [Improvement Plan & Implementation Details](#6-improvement-plan--implementation-details)
7. [Migration Strategy](#7-migration-strategy)
8. [Risk Assessment](#8-risk-assessment)

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

In LHA, `session.state["selected_model"]` is used to persist model overrides across turns. However, as noted in our architectural decision, persistent session state overrides are ill-suited for GitHub event webhooks where event complexity fluctuates between heavy commands and routine updates within the same conversation session.

---

## 3. Side-by-Side Comparison

| Architectural Feature | `hannibal-hub-agents` Current | `adk-samples` (LHA) | Proposed Hybrid Architecture |
|-----------------------|--------------------------------|---------------------|-------------------------------|
| **Routing Strategy** | Event-type payload classification | Per-turn callback (`select_model_callback`) | **Hybrid Payload Heuristics + Per-Turn Context Callback** |
| **State Persistence** | Stateless | Persistent (`session.state["selected_model"]`) | **Strictly Stateless (No Session State Overrides)** |
| **Model Swapping** | Mutates `agent.model` at runtime | `DispatchingLlm` wrapper | **`DispatchingLlm` Wrapper** |
| **Context Awareness** | Static event payload rules | Session state flags | **Dynamic (Payload Rules + Token Size/Complexity Check)** |
| **Explicit Overrides** | None | Session state string | **Per-turn command flags (e.g., `/review --model=heavy`)** |
| **Fallback Cascade** | Descending tier retry loop | Single retry | **TPM Pacing + Fallback Cascade via Dispatcher** |

---

## 4. Issues in Current Implementation

1. **Dangerous Mutability (`agent.model` Hot-Swapping)**: Mutating `self._agent.model` directly before runner execution is brittle and non-thread-safe.
2. **Context Blindness**: Payload-only classification misses situations where a "routine" comment contains a massive code snippet or complex query requiring a primary tier model.
3. **Rigid Tier Allocation**: Lacks inline mechanisms for users or admins to specify tier overrides per turn.

---

## 5. The Hybrid Model Routing Architecture

The Hybrid Router integrates fast payload heuristics with ADK's `before_model_callback` evaluation and `DispatchingLlm` execution wrapper.

```
           GitHub Webhook Event
                    │
                    ▼
    [ Layer 1: Fast Payload Heuristics ]
    (Inspect canonical event name & command triggers)
                    │
                    ▼
     [ Initial Tier Assignment Strategy ]
                    │
                    ▼
   [ Layer 2: ADK before_model_callback ]
   ├─ Check explicit per-turn overrides (e.g. /review model=pro)
   ├─ Inspect context token size & thread depth
   └─ Promote lightweight -> primary if context > threshold
                    │
                    ▼
      [ Layer 3: DispatchingLlm Wrapper ]
    ├─ Routes request to target Gemini model backend
    └─ Integrates TPM sliding window & fallback cascade
```

### Key Capabilities of the Hybrid Router:
1. **Payload-First Fast Path**: Webhook metadata quickly determines the base tier (e.g., `pull_request.opened` -> Primary; `label.added` -> Lightweight) without processing full history first.
2. **Context & Token Size Adjustment**: The `before_model_callback` checks prompt length. If a casual comment includes a 5,000 token log dump, it automatically promotes the turn to the primary model tier.
3. **Explicit Per-Turn Overrides**: Slash commands can include explicit tier hints (e.g. `/review --tier=heavy` or `/create --fast`) which override heuristic defaults for that single execution without polluting session state.
4. **Clean Abstraction**: `DispatchingLlm` handles backend routing cleanly without mutating `agent.model`.

---

## 6. Improvement Plan & Implementation Details

### 6.1 `DispatchingLlm` Interface

```python
class DispatchingLlm(BaseLlm):
    """LLM Wrapper routing per-call to lightweight or primary models without mutating agent.model."""

    def __init__(self, primary_model: BaseLlm, lightweight_model: BaseLlm):
        self.primary_model = primary_model
        self.lightweight_model = lightweight_model

    async def generate_response(self, prompt: str, session: Session, **kwargs) -> LlmResponse:
        model_tier = session.temp_state.get("turn_model_tier", "primary")
        selected = self.primary_model if model_tier == "primary" else self.lightweight_model
        return await selected.generate_response(prompt, session, **kwargs)
```

### 6.2 `before_model_callback` Hybrid Router

```python
async def hybrid_model_router_callback(callback_context: CallbackContext) -> None:
    """Per-turn callback evaluating payload heuristics, token length, and command overrides."""
    event_data = callback_context.session.temp_state.get("event_data", {})
    
    # Step 1: Base Payload Heuristics
    tier = _select_model_for_event(event_data)

    # Step 2: Context Token Length Check (Promote if prompt > 4000 tokens)
    prompt_tokens = estimate_tokens(callback_context.prompt)
    if tier == "lightweight" and prompt_tokens > 4000:
        tier = "primary"

    # Step 3: Temporary Per-Turn Assignment (Stateless)
    callback_context.session.temp_state["turn_model_tier"] = tier
```

---

## 7. Migration Strategy

1. **Phase 1 (Preparation)**: Create `DispatchingLlm` and unit tests in `webhook_agent.py`.
2. **Phase 2 (Hybrid Callback Integration)**: Implement `hybrid_model_router_callback` combining payload heuristics and prompt token checks.
3. **Phase 3 (Deprecate Hot-Swapping)**: Replace `self._agent.model = Gemini(...)` with `DispatchingLlm`.
4. **Phase 4 (Validation)**: Run unit and integration tests verifying stateless per-event routing under concurrent multi-turn workloads.

---

## 8. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Callback latency overhead | Minimal (<5ms) | Pre-compute payload heuristics; keep token estimation lightweight |
| Fallback loop complexity | Medium | Retain existing TPM descending cascade logic inside `DispatchingLlm` |
| Session state bleed | High | Strictly store turn-level tier decisions in transient per-turn state (`temp_state`), never in persistent `session.state` |
