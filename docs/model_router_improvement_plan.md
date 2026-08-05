# Dynamic Model Router Improvement Plan

> Comparing `hannibal-hub-agents` event-type-based model routing vs `adk-samples` patterns

---

## Executive Summary

Our dynamic model router (`_select_model_for_event()` in `webhook_agent.py`) classifies incoming webhook events by complexity and routes them to different models: heavy events (PR opens, slash commands, @mentions) go to the primary model, while routine events (closes, casual comments, label changes) go to a lightweight model. This saves quota and reduces latency for simple events.

Google's `adk-samples` repository takes a different approach. The "Software Bug Assistant" uses a single hardcoded model with no routing at all. The **Long Horizon Harness (LHA)** uses a `select_model_callback` (`before_model_callback`) that resolves the model per-turn via a precedence chain: `session.state["selected_model"]` > `LHA_ROOT_MODEL` env var > `DEFAULT_MODEL_NAME`. It also uses a `DispatchingLlm` wrapper that routes per-call to the right backend without recreating the Runner.

**These approaches are complementary.** Our event-type classification is a smart default selector; the LHA's per-session state pattern enables overrides. Combining them would give us: event-type-based defaults + per-session overrides + no Runner recreation + model capability awareness.

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

Our router is the `_select_model_for_event()` function (line 184 of `webhook_agent.py`). It classifies each webhook event as "heavy" or "routine" and selects the appropriate model tier. The selected model is then hot-swapped onto the live Agent before the ADK Runner processes the event.

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

### Call Site (line 988)

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

### Event Classification Table

| Event Type | Classification | Model | Rationale |
|------------|---------------|-------|-----------|
| `pull_request.opened` | Heavy | `gemini-3.6-flash` | Needs full code review, diff analysis |
| `issue_comment.created` with `/review` | Heavy | `gemini-3.6-flash` | Explicit code review request |
| `issue_comment.created` with `/create` | Heavy | `gemini-3.6-flash` | PR description generation |
| `issue_comment.created` with `/resolve` | Heavy | `gemini-3.6-flash` | Conflict resolution |
| `issue_comment.created` with `/help` | Heavy | `gemini-3.6-flash` | Help command |
| `issue_comment.created` with `@hannibal-hub-agents` | Heavy | `gemini-3.6-flash` | Direct bot mention |
| `issue_comment.created` (casual) | Routine | `gemini-3.5-flash-lite` | Simple acknowledgment |
| `pull_request.closed` | Routine | `gemini-3.5-flash-lite` | Lifecycle event, no action needed |
| `pull_request.reopened` | Routine | `gemini-3.5-flash-lite` | Lifecycle event |
| `pull_request_review_comment.created` (casual) | Routine | `gemini-3.5-flash-lite` | Simple review comment |
| `pull_request_review.created` | Routine | `gemini-3.5-flash-lite` | Review submitted |

### Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `GEMMA_MODEL` | `gemini-3.6-flash` | Primary model for heavy events |
| `GEMMA_LIGHTWEIGHT_MODEL` | `gemini-3.5-flash-lite` | Lightweight model for routine events |
| `ENABLE_DYNAMIC_MODEL_ROUTING` | `1` | Kill switch — when `0`, all events go to primary |
| `GEMMA_MODEL_MAX_RETRIES` | `5` | Max retries in the fallback cascade |
| `GEMMA_MODEL_FALLBACK` | `gemini-3.5-flash-lite` | Fallback model for the error cascade |

### Test Coverage

`TestDynamicModelRouting` in `test_agent_core.py` (6 tests):

| Test | Event | Expected Model |
|------|-------|----------------|
| `test_pull_request_opened_routes_to_primary_model` | `pull_request.opened` | `gemini-3.6-flash` |
| `test_slash_command_comment_routes_to_primary_model` | `issue_comment.created` with `/review` | `gemini-3.6-flash` |
| `test_bot_mention_comment_routes_to_primary_model` | `pull_request_review_comment.created` with `@hannibal-hub-agents` | `gemini-3.6-flash` |
| `test_routine_comment_routes_to_lightweight_model` | `issue_comment.created` (casual) | `gemini-3.5-flash-lite` |
| `test_pull_request_closed_routes_to_lightweight_model` | `pull_request.closed` | `gemini-3.5-flash-lite` |
| `test_disabled_dynamic_routing_forces_primary_model` | `pull_request.closed` (routing disabled) | `gemini-3.6-flash` |

---

## 2. adk-samples Reference Implementations

### Software Bug Assistant — No Dynamic Routing

The Software Bug Assistant (`python/agents/software-bug-assistant/`) uses a single hardcoded model:

```python
root_agent = Agent(
    model="gemini-2.5-flash",
    name="software_assistant",
    instruction=agent_instruction,
    tools=tools,
)
```

No event-type classification, no model selection logic, no fallback. Every request goes to the same model. This is the simplest possible approach — fine for a demo, not for a production webhook agent with quota constraints.

### Long Horizon Harness (LHA) — Per-Turn Model Resolution

The LHA (`core/python/long-horizon-harness/`) implements dynamic model routing through a 4-layer architecture:

#### Layer 1: Model Registry (`horizon/models/registry.py`)

A lazy registry with `ModelDescriptor` entries — adding a model is one table entry:

```python
@dataclass(frozen=True)
class ModelDescriptor:
    name: str
    factory: Callable[[], BaseLlm]       # lazy construction
    capabilities: ModelCapabilities       # media limits, content hooks
    input_token_limit: int               # for adaptive compaction

class _LazyModelRegistry:
    """Lazy-built singleton — backends constructed on first access."""
    def get(self, name: str) -> BaseLlm:
        if name not in self._cache:
            self._cache[name] = self._descriptors[name].factory()
        return self._cache[name]

MODEL_REGISTRY = _LazyModelRegistry(_MODELS)
```

#### Layer 2: DispatchingLlm (`horizon/models/dispatcher.py`)

A single `BaseLlm` subclass that routes per-call — no Runner recreation:

```python
class DispatchingLlm(BaseLlm):
    async def generate_content_async(self, request, llm_request):
        model_name = llm_request.model or DEFAULT_MODEL_NAME
        backend = self._registry.get(model_name)
        caps = self._registry.capabilities(model_name)
        if caps.prepare_contents:
            llm_request.contents = caps.prepare_contents(llm_request.contents)
        return await backend.generate_content_async(request, llm_request)
```

#### Layer 3: select_model_callback (`horizon/models/selector.py`)

A `before_model_callback` that resolves the model per-turn via precedence:

```python
async def select_model_callback(callback_context, llm_request):
    state = callback_context.state
    selected = state.get("selected_model")
    
    if selected and selected in MODEL_REGISTRY:
        llm_request.model = selected          # session state wins
    elif env_model := os.environ.get("LHA_ROOT_MODEL"):
        if env_model in MODEL_REGISTRY:
            llm_request.model = env_model     # env var fallback
    else:
        llm_request.model = DEFAULT_MODEL_NAME  # hardcoded default
    
    apply_compaction_threshold(callback_context, llm_request.model)
```

#### Layer 4: Adaptive Compaction (`horizon/context/compaction_threshold.py`)

Recalculates the compaction threshold based on the active model's context window:

```python
DEFAULT_WINDOW_FRACTION = 0.75

def compaction_token_threshold(input_token_limit: int) -> int:
    return max(1, int(input_token_limit * _window_fraction()))
```

#### Slash Command (`horizon/commands/__init__.py`)

Users can switch models mid-session:

```python
def _model(args: str, state: dict) -> str:
    model_name = args.strip()
    if model_name not in MODEL_REGISTRY:
        return f"Unknown model '{model_name}'. Available: {list(MODEL_REGISTRY)}"
    state["selected_model"] = model_name
    return f"Switched to {model_name}"
```

#### Tests (`tests/unit/models/test_selector.py`)

```python
async def test_state_wins_over_env(monkeypatch): ...
async def test_env_used_when_state_unset(monkeypatch): ...
async def test_hardcoded_default_when_no_state_no_env(monkeypatch): ...
async def test_unknown_state_value_falls_back_to_default(monkeypatch): ...
```

---

## 3. Side-by-Side Comparison

| Aspect | Our `_select_model_for_event()` | LHA `select_model_callback` |
|--------|-------------------------------|---------------------------|
| **Routing strategy** | Event-type classification (heavy vs routine) | Per-turn precedence resolution (state > env > default) |
| **Selection trigger** | Webhook event type + comment content | Every turn, via `before_model_callback` |
| **Model switching** | Hot-swap `self._agent.model = Gemini(...)` | `DispatchingLlm` routes per-call — no hot-swap |
| **Runner recreation** | No (just hot-swap) — but fallback cascade does recreate | No — `DispatchingLlm` handles everything |
| **Model registry** | Two env vars (`GEMMA_MODEL` + `GEMMA_LIGHTWEIGHT_MODEL`) | `MODEL_REGISTRY` with `ModelDescriptor` + capabilities |
| **Capability awareness** | None — models differ only by name | `ModelCapabilities` with media limits + `prepare_contents` |
| **Per-session override** | None — event type is the only selector | `session.state["selected_model"]` persists across turns |
| **User control** | None (env-only) | `/model` slash command |
| **Kill switch** | `ENABLE_DYNAMIC_MODEL_ROUTING` env var | N/A (always on, but default is safe) |
| **Classification logic** | Hardcoded if/elif chain on canonical event + comment body | No classification — just precedence resolution |
| **Test coverage** | 6 tests for event classification | 4 tests for precedence resolution |
| **Compaction** | Not implemented | Adaptive: `0.75 * input_token_limit(model)` per turn |
| **Code location** | Inline in 1109-line `webhook_agent.py` | Separated into `models/` package |

### Key Insight: Complementary Approaches

Our event-type classification and the LHA's per-session state resolution solve **different problems**:

- **Our approach** answers: "Which model should this *type* of event use?"
- **LHA's approach** answers: "Which model should this *session* use?"

Combining them gives us: "Use the event-type-appropriate model by default, but allow per-session overrides."

---

## 4. Issues in Current Implementation

### 4.1 Hot-Swap Instead of Declarative Routing

The selected model is applied via `self._agent.model = Gemini(model=selected_model)` (line 997). This works, but it's an imperative mutation of the Agent object. The LHA's `DispatchingLlm` approach is cleaner — the model is stamped on `llm_request.model` by a `before_model_callback`, and the `DispatchingLlm` routes per-call. No Agent mutation needed.

### 4.2 No Model Registry

Models are referenced by string names scattered across env vars. There's no central registry that knows each model's capabilities (context window, media support, TPM limits). Adding a new model means editing the env var defaults and hoping the string matches a valid model name. The LHA's `MODEL_REGISTRY` with `ModelDescriptor` entries is a single source of truth.

### 4.3 No Per-Session Override

If a particular PR needs a stronger model (e.g., a complex codebase with tricky merge conflicts), there's no way to pin a model for that session. The event-type classification is the only selector. The LHA's `session.state["selected_model"]` would allow per-PR overrides that persist across turns.

### 4.4 Classification Logic is Hardcoded

The event-type classification is a hardcoded if/elif chain. Adding a new "heavy" event type (e.g., `pull_request.synchronize` for force-pushes) means editing the function. There's no configuration file or registry of event-type-to-model mappings.

### 4.5 Missing Event Types in Classification

Several event types are not explicitly classified and fall through to the lightweight model by default:

| Event Type | Currently Routes To | Should It? |
|------------|-------------------|------------|
| `pull_request.synchronize` | Lightweight | **Heavy** — force-push may need re-review |
| `pull_request.ready_for_review` | Lightweight | **Heavy** — draft to ready transition |
| `pull_request_review_comment.created` with code suggestion | Lightweight | **Heavy** — may need model to analyze code |
| `pull_request_review.requested` | Lightweight | Maybe — review requested from bot |
| `issue_comment.created` with `/deploy` | Lightweight | **Heavy** — deployment command |

### 4.6 No Capability-Aware Content Preparation

When routing to the lightweight model, we don't adjust the content. If the event payload contains images or media that the lightweight model can't handle, it will fail. The LHA's `ModelCapabilities.prepare_contents` hook would strip or transform content per model.

### 4.7 Search Sub-Agent Model Inconsistency

The search sub-agent (line 578) uses `GEMMA_MODEL` env var with a different default (`gemma-4-31b-it`) than the main agent chain (which defaults to `gemini-3.6-flash`):

```python
search_sub_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMMA_MODEL", "gemma-4-31b-it"),  # ← different default!
    ...
)
```

If `GEMMA_MODEL` is not set, the main agent uses `gemini-3.6-flash` while the search sub-agent uses `gemma-4-31b-it` — a model not even in the fallback chain.

### 4.8 Duplicate Constants (Bug)

`_MAX_RETRIES`, `_FALLBACK_MODEL`, and `_is_transient_error()` are all defined **twice** in the same file:

| Constant | First definition | Second definition (overrides) | Different? |
|----------|-------------------|-------------------------------|------------|
| `_MAX_RETRIES` | Line 160 | Line 626 | No |
| `_FALLBACK_MODEL` | Line 184: default `gemini-3.5-flash-lite` | Line 627: default `gemma-4-26b-a4b-it` | **YES** |
| `_is_transient_error()` | Line 222: `GenAIServerError` + string matching | Line 630: `GenAIServerError` only | **YES** — less robust |

### 4.9 Runner Recreation in Fallback Cascade

`_create_fallback_agent()` (line 716) recreates the entire `Runner` object on every model cascade in the error fallback chain. This is separate from the event-type routing but affects the same Agent object.

### 4.10 Fixed Token Thresholds

Token caps (`MAX_INPUT_TOKENS=3500`, `MAX_DIFF_TOKENS=2500`) are fixed regardless of which model is active. The lightweight model (`gemini-3.5-flash-lite`) and the primary model (`gemini-3.6-flash`) both have 1M context windows, but the caps artificially limit all models to the same budget.

---

## 5. Improvement Plan

### Phase 1: Extract Model Registry (Structural Foundation)

#### 5.1 Create Model Registry Package

**New file:** `src/webhook_agent/models/__init__.py`

```python
"""Model registry and routing for the webhook agent."""
from .registry import MODEL_REGISTRY, ModelDescriptor
from .capabilities import ModelCapabilities
from .selector import select_model_callback, DEFAULT_MODEL_NAME
from .event_router import select_model_for_event, HEAVY_EVENTS, HEAVY_COMMANDS

__all__ = [
    "MODEL_REGISTRY", "ModelDescriptor", "ModelCapabilities",
    "select_model_callback", "DEFAULT_MODEL_NAME",
    "select_model_for_event", "HEAVY_EVENTS", "HEAVY_COMMANDS",
]
```

**New file:** `src/webhook_agent/models/registry.py`

```python
"""Lazy model registry with ModelDescriptor entries."""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Callable
from google.adk.models import Gemini
from google.adk.models.base_llm import BaseLlm
from .capabilities import ModelCapabilities


@dataclass(frozen=True)
class ModelDescriptor:
    name: str
    factory: Callable[[], BaseLlm]
    capabilities: ModelCapabilities
    input_token_limit: int
    tpm: int
    rpd: int


def _build_models() -> dict[str, ModelDescriptor]:
    return {
        "gemini-3.5-flash-lite": ModelDescriptor(
            name="gemini-3.5-flash-lite",
            factory=lambda: Gemini(model="gemini-3.5-flash-lite"),
            capabilities=ModelCapabilities(input_token_limit=1_000_000),
            input_token_limit=1_000_000, tpm=4_000_000, rpd=150_000,
        ),
        "gemini-3.6-flash": ModelDescriptor(
            name="gemini-3.6-flash",
            factory=lambda: Gemini(model="gemini-3.6-flash"),
            capabilities=ModelCapabilities(input_token_limit=1_000_000),
            input_token_limit=1_000_000, tpm=2_000_000, rpd=10_000,
        ),
        "gemini-2.5-flash": ModelDescriptor(
            name="gemini-2.5-flash",
            factory=lambda: Gemini(model="gemini-2.5-flash"),
            capabilities=ModelCapabilities(input_token_limit=1_000_000),
            input_token_limit=1_000_000, tpm=1_000_000, rpd=10_000,
        ),
        "gemma-4-26b": ModelDescriptor(
            name="gemma-4-26b",
            factory=lambda: Gemini(model="gemma-4-26b"),
            capabilities=ModelCapabilities(input_token_limit=8_000),
            input_token_limit=8_000, tpm=16_000, rpd=14_400,
        ),
    }


class _LazyModelRegistry:
    def __init__(self, descriptors):
        self._descriptors = descriptors
        self._cache: dict[str, BaseLlm] = {}

    def get(self, name: str) -> BaseLlm | None:
        if name not in self._descriptors:
            return None
        if name not in self._cache:
            self._cache[name] = self._descriptors[name].factory()
        return self._cache[name]

    def capabilities(self, name: str) -> ModelCapabilities:
        desc = self._descriptors.get(name)
        return desc.capabilities if desc else ModelCapabilities()

    def names(self) -> list[str]:
        return list(self._descriptors.keys())


MODEL_REGISTRY = _LazyModelRegistry(_build_models())
```

**New file:** `src/webhook_agent/models/capabilities.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ModelCapabilities:
    max_image_count: int = 0
    max_video_length_seconds: int = 0
    max_audio_length_seconds: int = 0
    input_token_limit: int = 1_000_000
    prepare_contents: Callable | None = None
```

---

### Phase 2: Refactor Event-Type Router (Keep the Good Logic)

#### 5.2 Move `_select_model_for_event()` to `models/event_router.py`

**New file:** `src/webhook_agent/models/event_router.py`

```python
"""Event-type-based model routing.

Classifies webhook events by complexity and routes them to the appropriate
model tier: heavy events to the primary model, routine events to the
lightweight model.

This is our unique value-add — the LHA doesn't have event-type classification.
Combined with the LHA's per-session state override, we get:
  event-type default + per-session override.
"""
from __future__ import annotations
import logging
import os
from typing import Any

logger = logging.getLogger("webhook_agent.models")

# Event types that always need the primary model
HEAVY_EVENTS: set[str] = {
    "pull_request.opened",
    "pull_request.synchronize",      # force-push may need re-review
    "pull_request.ready_for_review",  # draft to ready transition
}

# Comment substrings that trigger primary model
HEAVY_COMMANDS: tuple[str, ...] = (
    "/review", "/create", "/resolve", "/help", "/deploy",
    "@hannibal-hub-agents",
)


def select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type and content.

    Routes heavy workloads to the primary model (GEMMA_MODEL), and routine
    lifecycle events to the lightweight model (GEMMA_LIGHTWEIGHT_MODEL).

    Precedence (when combined with per-session state):
    1. session.state["selected_model"] — per-session override (if set)
    2. Event-type classification — this function
    3. GEMMA_MODEL env var — fallback default
    """
    primary = os.environ.get("GEMMA_MODEL", "gemini-3.6-flash")
    lightweight = os.environ.get("GEMMA_LIGHTWEIGHT_MODEL", "gemini-3.5-flash-lite")

    # Kill switch
    if os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", "1") not in (
        "1", "true", "True",
    ):
        return primary

    canonical = event_data.get("canonical", "")
    raw = event_data.get("raw_payload", {})

    # Heavy: explicitly listed event types
    if canonical in HEAVY_EVENTS:
        return primary

    # Heavy: comments with slash commands or bot mentions
    if canonical.startswith("issue_comment.") or canonical.startswith(
        "pull_request_review_comment."
    ):
        comment_body = ""
        if isinstance(raw.get("comment"), dict):
            comment_body = raw["comment"].get("body") or ""
        if any(cmd in comment_body for cmd in HEAVY_COMMANDS):
            return primary

    # Routine: everything else
    return lightweight
```

**Key improvements:**
- `HEAVY_EVENTS` is now a configurable set — adding `pull_request.synchronize` and `pull_request.ready_for_review`
- `HEAVY_COMMANDS` is a tuple — adding `/deploy`
- Both are module-level constants that can be extended without editing the function body
- Function renamed to `select_model_for_event` (public, no underscore) since it's now in its own module

---

### Phase 3: Implement Combined Selector Callback (LHA-Inspired)

#### 5.3 Create `select_model_callback` that Combines Event-Type + Session State

**New file:** `src/webhook_agent/models/selector.py`

```python
"""Per-turn model selection callback.

Combines our event-type classification with the LHA's per-session state
override pattern.

Precedence:
1. session.state["selected_model"] — per-session override (if valid)
2. Event-type classification — heavy vs routine
3. GEMMA_MODEL env var — fallback default
"""
from __future__ import annotations
import logging
import os

from google.adk.models.llm_request import LlmRequest
from .registry import MODEL_REGISTRY
from .event_router import select_model_for_event

logger = logging.getLogger("webhook_agent.models")

DEFAULT_MODEL_NAME = os.environ.get("GEMMA_MODEL", "gemini-3.6-flash")


async def select_model_callback(callback_context, llm_request: LlmRequest) -> None:
    """Resolve which model to use this turn.

    Precedence:
    1. session.state["selected_model"] (if valid) — per-session override
    2. Event-type classification (heavy to primary, routine to lightweight)
    3. DEFAULT_MODEL_NAME
    """
    state = callback_context.state

    # 1. Per-session override (LHA pattern)
    selected = state.get("selected_model")
    if selected and MODEL_REGISTRY.get(selected) is not None:
        llm_request.model = selected
        logger.debug("Model from session state: %s", selected)
        return

    # 2. Event-type classification (our unique pattern)
    event_data = state.get("event_data", {})
    if event_data:
        event_model = select_model_for_event(event_data)
        if MODEL_REGISTRY.get(event_model) is not None:
            llm_request.model = event_model
            logger.debug("Model from event classification: %s", event_model)
            return

    # 3. Fallback to default
    if MODEL_REGISTRY.get(DEFAULT_MODEL_NAME) is None:
        logger.warning("Default model %s not in registry", DEFAULT_MODEL_NAME)
        names = MODEL_REGISTRY.names()
        llm_request.model = names[0] if names else "gemini-3.6-flash"
    else:
        llm_request.model = DEFAULT_MODEL_NAME
```

#### 5.4 Wire Up in WebhookAgent

**File:** `src/webhook_agent/webhook_agent.py`

```python
# Before (line 988):
selected_model = _select_model_for_event(event_data)
if self._current_model_name != selected_model:
    self._current_model_name = selected_model
    self._agent.model = Gemini(model=selected_model)  # hot-swap

# After: store event_data in session state, let callback handle selection
session = await self._session_service.get_session(...)
if session:
    session.state["event_data"] = event_data  # callback reads this

# Agent is created once with before_model_callback — no hot-swap needed:
self._agent = Agent(
    name="webhook_agent",
    model=DispatchingLlm(),  # routes per-call
    before_model_callback=[select_model_callback],
    ...
)
```

---

### Phase 4: Implement DispatchingLlm (No More Hot-Swap)

#### 5.5 Create DispatchingLlm Wrapper

**New file:** `src/webhook_agent/models/dispatcher.py`

```python
"""DispatchingLlm — routes per-call to the right backend.

Replaces the imperative hot-swap pattern. The before_model_callback
stamps llm_request.model each turn; this class delegates accordingly.
"""
from __future__ import annotations
import logging

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai.types import GenerateContentResponse

from .registry import MODEL_REGISTRY
from .selector import DEFAULT_MODEL_NAME

logger = logging.getLogger("webhook_agent.models")


class DispatchingLlm(BaseLlm):
    """Routes generate_content_async to the backend named in llm_request.model."""

    async def generate_content_async(
        self, request: LlmRequest, llm_request: LlmRequest
    ) -> GenerateContentResponse:
        model_name = llm_request.model or DEFAULT_MODEL_NAME
        backend = MODEL_REGISTRY.get(model_name)

        if backend is None:
            logger.warning("Model %s not in registry; using default", model_name)
            backend = MODEL_REGISTRY.get(DEFAULT_MODEL_NAME)
            llm_request.model = DEFAULT_MODEL_NAME

        if backend is None:
            raise RuntimeError(f"No model backend available for {model_name}")

        # Apply per-model content preparation
        caps = MODEL_REGISTRY.capabilities(llm_request.model)
        if caps.prepare_contents:
            llm_request.contents = caps.prepare_contents(llm_request.contents)

        return await backend.generate_content_async(request, llm_request)
```

---

### Phase 5: Fix Bugs and Clean Up

#### 5.6 Remove Duplicate Constants

Remove the second definitions at lines 626-640 of `webhook_agent.py`. Keep the first definitions at lines 160-238 (more robust `_is_transient_error()` and correct `_FALLBACK_MODEL` default).

#### 5.7 Fix Search Sub-Agent Model

```python
# Before (line 578):
search_sub_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMMA_MODEL", "gemma-4-31b-it"),  # inconsistent!
    ...
)

# After:
from .models.registry import MODEL_REGISTRY
search_sub_agent = Agent(
    name="search_agent",
    model=MODEL_REGISTRY.get(
        os.environ.get("GEMMA_MODEL", "gemini-3.6-flash")
    ) or Gemini(model="gemini-3.6-flash"),
    ...
)
```

#### 5.8 Remove Deprecated Stub

Archive `src/webhook_agent/gemma_planner.py` (13-line deprecated stub).

---

### Phase 6: Expand Test Coverage

#### 5.9 Add Tests for New Event Types and Combined Routing

**New file:** `src/webhook_agent/tests/test_model_router.py`

```python
"""Tests for the model routing logic."""
import pytest
from webhook_agent.models.event_router import (
    select_model_for_event, HEAVY_EVENTS, HEAVY_COMMANDS,
)


class TestSelectModelForEvent:
    def test_pull_request_opened_routes_to_primary(self):
        assert select_model_for_event({"canonical": "pull_request.opened"}) == "gemini-3.6-flash"

    def test_pull_request_synchronize_routes_to_primary(self):
        """NEW: force-push should use primary model."""
        assert select_model_for_event({"canonical": "pull_request.synchronize"}) == "gemini-3.6-flash"

    def test_pull_request_ready_for_review_routes_to_primary(self):
        """NEW: draft to ready transition should use primary model."""
        assert select_model_for_event({"canonical": "pull_request.ready_for_review"}) == "gemini-3.6-flash"

    def test_slash_command_routes_to_primary(self):
        event = {"canonical": "issue_comment.created", "raw_payload": {"comment": {"body": "/review this"}}}
        assert select_model_for_event(event) == "gemini-3.6-flash"

    def test_deploy_command_routes_to_primary(self):
        """NEW: /deploy should use primary model."""
        event = {"canonical": "issue_comment.created", "raw_payload": {"comment": {"body": "/deploy staging"}}}
        assert select_model_for_event(event) == "gemini-3.6-flash"

    def test_bot_mention_routes_to_primary(self):
        event = {"canonical": "issue_comment.created", "raw_payload": {"comment": {"body": "@hannibal-hub-agents help"}}}
        assert select_model_for_event(event) == "gemini-3.6-flash"

    def test_routine_comment_routes_to_lightweight(self):
        event = {"canonical": "issue_comment.created", "raw_payload": {"comment": {"body": "Looks good!"}}}
        assert select_model_for_event(event) == "gemini-3.5-flash-lite"

    def test_pull_request_closed_routes_to_lightweight(self):
        assert select_model_for_event({"canonical": "pull_request.closed"}) == "gemini-3.5-flash-lite"

    def test_disabled_routing_forces_primary(self, monkeypatch):
        monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "0")
        assert select_model_for_event({"canonical": "pull_request.closed"}) == "gemini-3.6-flash"

    def test_empty_comment_body_routes_to_lightweight(self):
        """Edge case: empty comment body should not crash."""
        event = {"canonical": "issue_comment.created", "raw_payload": {"comment": {"body": ""}}}
        assert select_model_for_event(event) == "gemini-3.5-flash-lite"

    def test_missing_comment_key_routes_to_lightweight(self):
        """Edge case: missing comment key should not crash."""
        event = {"canonical": "issue_comment.created", "raw_payload": {}}
        assert select_model_for_event(event) == "gemini-3.5-flash-lite"

    def test_unknown_event_routes_to_lightweight(self):
        """Unknown event types default to lightweight."""
        assert select_model_for_event({"canonical": "unknown.event"}) == "gemini-3.5-flash-lite"


class TestSelectModelCallback:
    """Tests for the combined event-type + session-state selector."""

    @pytest.mark.asyncio
    async def test_session_state_overrides_event_type(self):
        """Per-session override should win over event-type classification."""
        ...

    @pytest.mark.asyncio
    async def test_event_classification_used_when_no_state(self):
        """Event-type classification should be used when no session override."""
        ...

    @pytest.mark.asyncio
    async def test_default_used_when_no_state_no_event(self):
        """Default model used when neither state nor event data available."""
        ...

    @pytest.mark.asyncio
    async def test_unknown_session_state_falls_back_to_event(self):
        """Stale session state should fall through to event classification."""
        ...
```

---

### Phase 7: Documentation

#### 5.10 Update MODEL_CHAIN.md

Update to document:
- The event-type classification table (heavy vs routine)
- The combined precedence chain (session state > event type > env > default)
- The `HEAVY_EVENTS` and `HEAVY_COMMANDS` configuration
- How to add a new event type (add to `HEAVY_EVENTS` set)
- How to add a new model (one `ModelDescriptor` entry)

---

## 6. Migration Strategy

### Step 1: Extract Model Package (2-3 hours, low risk)

1. Create `src/webhook_agent/models/` package with `registry.py`, `capabilities.py`
2. Move `get_model_chain()` to `models/registry.py`
3. Import from new package in `webhook_agent.py`
4. Run existing tests to verify no regression

### Step 2: Refactor Event Router (1-2 hours, low risk)

1. Move `_select_model_for_event()` to `models/event_router.py`
2. Add `pull_request.synchronize` and `pull_request.ready_for_review` to `HEAVY_EVENTS`
3. Add `/deploy` to `HEAVY_COMMANDS`
4. Update import in `webhook_agent.py`
5. Run `TestDynamicModelRouting` tests

### Step 3: Implement Combined Selector (2-3 hours, medium risk)

1. Create `models/selector.py` with `select_model_callback`
2. Store `event_data` in session state before running the agent
3. Wire `select_model_callback` as `before_model_callback` on the Agent
4. Remove the hot-swap call at line 997
5. Test with real webhook events

### Step 4: Implement DispatchingLlm (2-3 hours, medium risk)

1. Create `models/dispatcher.py`
2. Replace `Gemini(model=...)` with `DispatchingLlm()` in Agent construction
3. Remove `_advance_model_chain()` and `_create_fallback_agent()` from WebhookAgent
4. Move fallback logic into `DispatchingLlm`
5. Test with real webhook events

### Step 5: Fix Bugs (1 hour, zero risk)

1. Remove duplicate constants (lines 626-640)
2. Fix search sub-agent model default
3. Archive `gemma_planner.py`

### Step 6: Expand Tests (1-2 hours, low risk)

1. Add tests for new event types (`synchronize`, `ready_for_review`)
2. Add tests for new commands (`/deploy`)
3. Add edge case tests (empty body, missing keys)
4. Add tests for combined selector (session state + event type)
5. Run `scripts/ruff-all.sh`

### Step 7: Documentation (1 hour, zero risk)

1. Update `MODEL_CHAIN.md`
2. Update `AGENTS.md` if needed

---

## 7. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `select_model_callback` breaks event routing | Medium | High | Keep `_select_model_for_event()` as fallback; test with real events |
| `DispatchingLlm` breaks fallback cascade | Medium | High | Keep old hot-swap code in git history; test 429 scenarios |
| New event types misclassified | Low | Medium | Default to lightweight (safe — just slower, not broken) |
| Session state override stale | Low | Low | Fall through to event classification on unknown model |
| Search sub-agent model change breaks search | Low | Medium | Test search with real queries before deploying |

### Rollback Strategy

Every phase is independently deployable:

1. **Phase 1-2**: `models/` package is additive — old code still works
2. **Phase 3-4**: If `select_model_callback` or `DispatchingLlm` breaks, revert `WebhookAgent.__init__` to use `Gemini(model=...)` directly and restore the hot-swap call
3. **Phase 5-7**: Bug fixes and tests — no risk

Git tag before each phase: `model-router-phase1`, `model-router-phase2`, etc.

---

## Summary: What We're Adopting from LHA

| LHA Pattern | Adopting? | Rationale |
|-------------|-----------|-----------|
| `DispatchingLlm` (per-call routing) | Yes | Eliminates hot-swap; cleaner than `agent.model = Gemini(...)` |
| `select_model_callback` (`before_model_callback`) | Yes | Declarative per-turn model resolution |
| `MODEL_REGISTRY` with `ModelDescriptor` | Yes | Single source of truth for model metadata |
| `ModelCapabilities` | Yes | Per-model content preparation and token limits |
| `session.state["selected_model"]` per-session override | Yes | Allows per-PR model pinning |
| Adaptive compaction threshold | Future | Needs `EventsCompactionConfig` adoption first |
| `/model` slash command | No | Webhook agent is non-interactive |
| Lazy registry construction | Yes | Avoids instantiating all backends at startup |
| Comprehensive selector tests | Yes | Critical for routing reliability |

### What We're Keeping (Our Unique Value)

| Our Pattern | Keeping? | Rationale |
|-------------|----------|-----------|
| Event-type classification (`_select_model_for_event`) | **Yes** | Our unique value — LHA doesn't have this |
| `HEAVY_EVENTS` / `HEAVY_COMMANDS` config | **Yes** | Makes classification extensible |
| `ENABLE_DYNAMIC_MODEL_ROUTING` kill switch | **Yes** | Safety valve for production |
| TPM-descending fallback chain | Yes | Still needed for free-tier quota management |
| `SlidingWindowPacer` | Yes | Still needed for Gemma 4's 16k TPM limit |
| `count_tokens_exact()` | Yes | Still needed for input token safety |
| Per-PR session derivation | Yes | Unrelated to model routing |
| Policy gates (bot/read-only/mutation) | Yes | Unrelated to model routing |

### The Combined Architecture

```
Webhook Event arrives
    |
    v
session.state["event_data"] = event_data  <- store for callback
    |
    v
ADK Runner.run_async()
    |
    v
before_model_callback: select_model_callback()
    |
    +- 1. session.state["selected_model"]? -> use it (per-session override)
    +- 2. select_model_for_event(event_data) -> event-type classification
    |      +- Heavy event? -> primary model
    |      +- Routine event? -> lightweight model
    +- 3. DEFAULT_MODEL_NAME -> fallback
    |
    v
DispatchingLlm.generate_content_async()
    |
    +- Read llm_request.model (stamped by callback)
    +- Look up backend in MODEL_REGISTRY
    +- Apply ModelCapabilities.prepare_contents
    +- Delegate to backend
    |
    v
On transient error -> TPM-descending fallback chain (kept as-is)