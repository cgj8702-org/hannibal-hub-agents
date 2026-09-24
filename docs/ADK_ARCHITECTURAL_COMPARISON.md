# 🏛️ ADK Architecture Deep-Dive & Modernization Comparison

> **Analysis Target:** `hannibal-hub-agents` vs `google/adk-samples` (`long-horizon-harness`)  
> **Status:** Architectural Comparison, Technical Debt Audit & Modernization Spec  
> **Author:** Antigravity & Pair Programming Partner  
> **Target Framework:** Google ADK (Agent Development Kit) 2.0+  

---

## 1. Executive Summary

An architectural audit of Google's flagship reference implementations in [`../adk-samples`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/adk-samples) (specifically [`core/python/long-horizon-harness`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/adk-samples/core/python/long-horizon-harness)) was conducted to evaluate our production [`webhook_agent`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent) and the orphaned [`src/token_optimized_agent`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/token_optimized_agent) prototype.

The audit revealed three major findings:
1. **The Toy Prototype Flaw:** The mock `src/token_optimized_agent` package used `ContextCacheConfig(min_tokens=2048)`. As documented in `long-horizon-harness`, Gemini 3* models enforce a hardcoded minimum cache threshold of **4,096 tokens**, making 2,048 completely dead, non-binding configuration.
2. **The Production Gap in `webhook_agent`:** `webhook_agent` runs on a bare `Runner` without an `App`, bypassing ADK's native context caching, event compaction, and plugin systems.
3. **The `SequentialAgent` Technical Debt:** `webhook_agent` still uses `SequentialAgent`, an ADK 1.x primitive deprecated in ADK 2.0+. In commit `e9c3799`, the deprecation warning was silenced via `filterwarnings` in `pyproject.toml` rather than refactoring to the modern graph-based `Workflow` engine.

---

## 2. Comprehensive Architectural Comparison Matrix

| Dimension | `adk-samples` (`long-horizon-harness`) | `hannibal-hub-agents` (`webhook_agent`) | Our Toy Showcase (`token_optimized_agent`) |
|:---|:---|:---|:---|
| **Runtime Wrapper** | **`App`** (`App(root_agent=..., plugins=...)`) | ❌ **Bare `Runner`** (`Runner(agent=SequentialAgent)`) | `App` (bare/minimal) |
| **Context Caching** | **`ContextCacheConfig(min_tokens=4096, ...)`** *(real Gemini 3* floor)* | ❌ **None** (every turn re-transmits all system rules) | `min_tokens=2048` *(dead config on Gemini 3!)* |
| **Tool Output Pruning** | **Deterministic zero-LLM pruner** (`tool_output_pruning.py`) | ❌ **None** (large tool outputs live in history forever) | Naive string truncation in callback |
| **Compaction Strategy** | **Structured 7-section anchored markdown template** + reference banner | ❌ **None** | Unanchored `LlmEventSummarizer` |
| **Multi-Agent Topology** | **Focused subagents** with isolated context windows | `SequentialAgent` *(deprecated in ADK 2.0+)* | Generic toy `analyst` / `lookup` agents |
| **Turn History Overflow** | Sliding window + token-budget countdown | Sessions accumulate all turns across PR comment exchanges | `MessagePruningPlugin` (hard turn count) |
| **Conditional Routing** | Dynamic execution routes | ❌ Linear pipeline; executes all sub-agents sequentially | ❌ Hardcoded coordinator |

---

## 3. Deep-Dive: Slaying the `SequentialAgent` Dinosaur

### The Crime Scene (Commit `e9c3799`)
On August 18, 2026, when upgrading `google-adk`, the team encountered:
```text
DeprecationWarning: SequentialAgent is deprecated in favor of Workflow.
```
Instead of updating the agent graph, `pyproject.toml` was modified to sweep the warning under the rug:
```toml
filterwarnings = [
    "ignore:.*LoopAgent is deprecated in favor of Workflow.*:DeprecationWarning",
    "ignore:.*SequentialAgent is deprecated in favor of Workflow.*:DeprecationWarning",
]
```

### Why `SequentialAgent` Fails in Production
1. **Zero Conditional Branching:**  
   `SequentialAgent` blindly executes every sub-agent in order. Even if `_pr_router` identifies that a PR is a documentation typo or non-code change, `SequentialAgent` drags execution through the heavy `_code_auditor` with its 17 tools and thinking mode, burning thousands of tokens.
2. **Context Bleed:**  
   Downstream agents (`_verdict_agent`) inherit the entire accumulated conversation history and raw diff from earlier agents, compounding token costs with zero informational gain.
3. **No Early Exits:**  
   Short-circuiting requires hacky exception throwing rather than clean graph termination.

### The ADK 2.0 Solution: Graph-Based `Workflow`
ADK 2.0 replaces linear sequences with a directed graph composed of **Nodes** and **Edges** (`from google.adk.workflow import Workflow`).

```
              ┌─────────┐
              │  START  │
              └────┬────┘
                   │
                   ▼
         ┌───────────────────┐
         │    _pr_router     │
         └─────────┬─────────┘
                   │
         Is code audit needed?
        ╱                     ╲
      YES                      NO (docs / chores / trivial)
      ╱                         ╲
     ▼                           ▼
┌──────────────────┐             │
│  _code_auditor   │             │
│ (17 tools / AST) │             │
└────────┬─────────┘             │
         │                       │
         └───────────┬───────────┘
                     │
                     ▼
          ┌─────────────────────┐
          │   _verdict_agent    │
          │ (AuditVerdict JSON) │
          └─────────────────────┘
```

#### Modern Workflow Implementation:
Routes are *emitted*, not predicated: `pr_router` writes its classification onto
the node's event via `EventActions.route` (`router_after_agent_callback`), and
the scheduler follows the matching edge.

```python
from google.adk.workflow import DEFAULT_ROUTE, START, Edge, Workflow

webhook_workflow = Workflow(
    name="webhook_agent",
    edges=[
        (START, self._pr_router),
        # Conditional edge: docs-only PRs skip straight to verdict generation.
        Edge(
            from_node=self._pr_router,
            to_node=self._verdict_agent,
            route=ROUTE_DEV_DOCS,
        ),
        # DEFAULT_ROUTE is the fail-safe: `minor_fix`, `core_backend`, an
        # unrecognized scope, or a router that emitted no route at all still
        # run the full audit.
        Edge(
            from_node=self._pr_router,
            to_node=self._code_auditor,
            route=DEFAULT_ROUTE,
        ),
        (self._code_auditor, self._verdict_agent),
    ],
)
```

`router_after_agent_callback` also writes `pr_scope_route` to state, because ADK
only emits the event carrying `actions.route` when the callback produces a state
delta. `verdict_agent` reads its inputs through optional template placeholders
(`{pr_scope?}`, `{code_review_analysis?}`) so the skipped-audit path renders
without raising `KeyError`.

---

## 4. Deep-Dive: Deterministic Zero-LLM Tool Pruning

In [`adk-samples/core/python/long-horizon-harness/horizon/context/tool_output_pruning.py`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/adk-samples/core/python/long-horizon-harness/horizon/context/tool_output_pruning.py), Google introduced a brilliant zero-cost context reclamation mechanism:

### The Problem in Multi-Turn PR Threads
When a developer comments on a PR after an initial review, `webhook_agent` re-attaches to the existing `Session`. Turn 1 already fetched the entire 10,000-token git diff. Turn 2 (answering a short user question) re-transmits the massive turn 1 diff over and over again, quickly blowing past Gemma's 15k/16k TPM ceiling.

### The Zero-Cost Reclamation Algorithm
Rather than invoking an expensive LLM summarizer on every turn:
1. **Walks events newest -> oldest:** Evaluates tool responses in reverse chronological order.
2. **Protects recent context:** Always leaves the last 3 turns and a 40,000-token recent budget completely untouched.
3. **Replaces stale tool outputs:** For older turns, replaces massive function responses with:
   ```text
   [output pruned to reclaim context — re-run the tool if needed]
   ```
4. **Protects critical tool types:** Never prunes skills, user clarifications, or subagent reports.
5. **Reclaims 20,000–50,000 tokens for FREE** before the LLM ever sees the request!

---

## 5. The Real 4,096-Token Floor for Gemini Context Caching

Our mock `src/token_optimized_agent` configured:
```python
context_cache_config = ContextCacheConfig(min_tokens=2048)  # ❌ INERT BUG
```
As explicitly documented by the ADK team in `long-horizon-harness/horizon/agent.py`:
> *"Gemini's own per-model floor (`gemini_context_cache_manager.py`'s `_minimum_cache_tokens`) is hardcoded to 4096 for any `gemini-3*` model, so 2048 here was dead config... 4096 documents the real floor instead of a smaller, never-binding number."*

### The Correct Production Configuration
```python
from google.adk.apps import App
from google.adk.agents.context_cache_config import ContextCacheConfig

app = App(
    name="webhook_agent",
    root_agent=webhook_workflow,
    context_cache_config=ContextCacheConfig(
        min_tokens=4096,     # Binds properly to Gemini 3* models
        ttl_seconds=1800,    # 30-minute cache window across webhook bursts
        cache_intervals=10,  # Refresh cadence
    ),
    plugins=[...],
)
```

---

## 6. Execution Roadmap

```
Step 1: Workspace Cleanup
  ├── Remove src/token_optimized_agent/
  ├── Remove tests/unit/test_token_optimization.py
  └── Prune pyproject.toml known-first-party entry

Step 2: Workflow Migration (Slay the SequentialAgent)
  ├── Convert SequentialAgent to ADK 2.0 Workflow graph
  ├── Add conditional edge skipping for docs/trivial changes
  └── Delete "ignore:.*SequentialAgent" from pyproject.toml

Step 3: App & Context Cache Promotion
  ├── Promote WebhookAgent runner to ADK App
  └── Configure ContextCacheConfig(min_tokens=4096, ttl_seconds=1800)

Step 4: Deterministic Tool Output Pruning
  ├── Implement tool_output_pruning callback/plugin
  └── Prevent multi-turn PR comment diff bloat
```
