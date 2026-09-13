# ⚡ ADK Token Optimization Architecture & Modernization Blueprint

> **System Designation:** `hannibal-hub-agents` / `webhook_agent`  
> **Status:** Architectural Blueprint & Implementation Specification  
> **Author:** Antigravity & Pair Programming Partner  
> **Target Framework:** Google ADK (Agent Development Kit) 2.0+  

---

## Executive Summary

The **Webhook Auditor Agent** (`webhook_agent`) is the critical production service in `hannibal-hub-agents`, operating continuously on Google Compute Engine (`hannibal-hub-free`) to review pull requests, process code comments, and post verdicts. 

Under real-world workloads, PR reviews frequently encounter free-tier rate limits (such as Gemma's 15,000–16,000 TPM ceiling). Large diffs combined with extensive system instructions can exhaust per-minute token capacity, causing rate-limiter pauses and worker queue delays.

Meanwhile, an earlier reference package ([`src/token_optimized_agent/`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/token_optimized_agent)) implemented toy token management patterns in isolation without connecting to production. 

**The Objective:** Decommission the isolated prototype and natively integrate **bleeding-edge Google ADK 2.0+ token optimization primitives** directly into `webhook_agent`.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Before: Raw Runner Execution                    │
│                                                                        │
│   Incoming Event ──▶ Bare Runner ──▶ SequentialAgent                   │
│                        │               ├── Re-sends full system prompt │
│                        │               ├── Re-sends raw 10k diff       │
│                        │               └── Context bleeds across turns │
│                        ▼                                               │
│             ⚠️ High TPM Quota Burn / Rate-Limiter Throttling          │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                     After: ADK 2.0+ App Architecture                   │
│                                                                        │
│   Incoming Event ──▶ ADK App Wrapper                                  │
│                        ├── ContextCacheConfig (Static Prefix Caching)  │
│                        ├── MessagePruningPlugin (Session Retention)    │
│                        └── Sub-Agent Isolation (include_contents=none) │
│                        ▼                                               │
│             ⚡ Up to 80% Input Token & Latency Reduction                │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 1. The Core Architectural Pillars

### Pillar 1: Native Context Caching (`ContextCacheConfig`) via `App`

#### Problem
`WebhookAgent` currently constructs a bare `Runner` (`Runner(agent=self._agent, ...)`). Because it bypasses the ADK `App` abstraction, it cannot leverage Google GenAI's transparent prefix caching. Every LLM turn re-transmits the entire system prompt, review guidelines, audit rubrics, and markdown templates.

#### Solution
Wrap the agent hierarchy in an ADK `App` with `ContextCacheConfig`:

```python
from google.adk.apps import App
from google.adk.agents.context_cache_config import ContextCacheConfig

webhook_app = App(
    name="webhook_agent",
    root_agent=root_agent,
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,     # Cache when system instructions + context exceed 2k tokens
        ttl_seconds=1800,    # 30-minute cache window across webhook bursts
        cache_intervals=5,   # Refresh cache cadence
    ),
    plugins=[...],
)
```

* **Impact:** Static system prompts and review templates are cached in Google Cloud GenAI infrastructure. Calls within the 30-minute window bypass full prompt token ingestion, slashing input token consumption and latency by up to **80%** on Gemini models.

---

### Pillar 2: Sub-Agent Scope Isolation (`include_contents="none"`)

#### Problem
In the current `SequentialAgent` workflow (`_pr_router` -> `_code_auditor` -> `_verdict_agent`), each sub-agent inherits the conversation history and raw diff payload from preceding agents. 
- The `_verdict_agent` only needs structured findings to emit the Pydantic `AuditVerdict`.
- Forcing it to re-read the 10,000-token raw git diff burns massive token quota with zero informational gain.

#### Solution
Isolate sub-agent context using `include_contents="none"` and template data dynamically from `state`:

```python
verdict_agent = Agent(
    name="verdict_generator",
    model=model_instance,
    instruction=(
        "You are the verdict evaluator. Based on the audit findings below, "
        "produce the structured AuditVerdict output.\n\n"
        "Audit Findings:\n{audit_findings}"
    ),
    include_contents="none",  # Do NOT inherit raw diff history or tool call logs
    output_schema=AuditVerdict,
    output_key="audit_verdict",
)
```

* **Impact:** Downstream agents process only synthesized findings (typically 300–800 tokens) rather than compounding history from earlier turns.

---

### Pillar 3: Tool Output Guardrails (`after_tool_callback`)

#### Problem
Tools such as `search_code`, `fetch_diff`, and `git_blame` can return unbounded outputs when processing large repositories or sweeping changes. Injecting a massive string directly into the LLM conversation turns causes immediate context blowouts.

#### Solution
Implement an `after_tool_callback` that enforces a strict character and token ceiling, truncating or summarizing oversized tool returns before they enter the model history:

```python
async def truncate_tool_output_callback(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any,
    context: ToolContext,
) -> Any:
    """Enforce token safety on tool returns."""
    MAX_TOOL_CHARS = 12_000  # ~3,000 tokens safe ceiling
    
    if isinstance(tool_output, str) and len(tool_output) > MAX_TOOL_CHARS:
        logger.warning(
            "⚠️ Tool %s output exceeded %d chars (%d chars); truncating with notice",
            tool_name,
            MAX_TOOL_CHARS,
            len(tool_output),
        )
        return (
            tool_output[:MAX_TOOL_CHARS]
            + f"\n\n[... Truncated {len(tool_output) - MAX_TOOL_CHARS} characters to preserve token quota ...]"
        )
    return tool_output
```

---

### Pillar 4: Multi-Turn Conversation Pruning (`MessagePruningPlugin`)

#### Problem
When a pull request thread has multiple comments or review cycles, all previous turns remain in the ADK `Session`. By turn 3 or 4, earlier diffs and comments are repeatedly transmitted on every request.

#### Solution
Integrate an ADK plugin to prune conversation events beyond the active working window while retaining key summary data in `session.state`:

```python
from google.adk.plugins import BasePlugin

class WebhookHistoryPruningPlugin(BasePlugin):
    """Retains only the latest N events in active model context for multi-turn sessions."""
    def __init__(self, max_events: int = 12):
        self.max_events = max_events

    async def before_model(self, context, request):
        if len(request.contents) > self.max_events:
            # Preserve system turn [0] and retain the most recent events
            request.contents = [request.contents[0]] + request.contents[-self.max_events:]
```

---

## 2. Workspace Cleanup & Decommissioning Plan

The standalone `src/token_optimized_agent/` directory will be decommissioned as its patterns are absorbed into `webhook_agent`.

### Files to Remove / Decommission
1. `src/token_optimized_agent/` (entire package):
   - `src/token_optimized_agent/app.py`
   - `src/token_optimized_agent/agent.py`
   - `src/token_optimized_agent/callbacks.py`
   - `src/token_optimized_agent/tools.py`
   - `src/token_optimized_agent/__init__.py`
2. `tests/unit/test_token_optimization.py`:
   - Outdated unit tests referencing the toy package.
3. `pyproject.toml`:
   - Remove `"token_optimized_agent"` from `known-first-party` isort list.

### Active Core Structure Post-Cleanup
```
src/
├── webhook_agent/     # Production Webhook Auditor with native ADK token optimization
├── feature_agent/     # Autonomous Feature Developer Engine
└── logic/             # Shared infrastructure (rate limiting, model factory, secrets)
```

---

## 3. Implementation Phasing

| Phase | Description | Deliverables |
|:---:|---|---|
| **Phase 1** | **Workspace Cleanup** | Remove `src/token_optimized_agent/` and associated unit tests. Clean up `pyproject.toml`. |
| **Phase 2** | **App & Caching Promotion** | Promote `webhook_agent` from bare `Runner` to `App` with `ContextCacheConfig`. |
| **Phase 3** | **Context & Scope Isolation** | Add `include_contents="none"` to secondary sub-agents, wire `truncate_tool_output_callback`. |
| **Phase 4** | **Verification & Benchmarking** | Validate full pytest test suite, run `ruff-all.sh`, and verify zero test regressions. |

---

## 4. Verification Standards

1. **Automated Unit Tests:** `uv run pytest src/webhook_agent/tests/ -q` must achieve 100% pass rate.
2. **Linting Compliance:** `bash scripts/ruff-all.sh` must report clean formatting and 0 errors.
3. **Zero-Bypass PR Deployment:** Changes must be submitted via pull request under author identity `cgj8702-agents <cgj8702-agents@users.noreply.github.com>` and approved by `hannibal-hub-agents[bot]`.
