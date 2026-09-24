# 🔬 `agents-cli` Scaffolding Experiment & Architecture Benchmark

> **System Designation:** `hannibal-hub-agents` / `webhook_agent` vs `google-agents-cli` v1.5.0  
> **Status:** Architecture Benchmark, Prototype Audit & Modernization Analysis  
> **Backlog Reference:** Issue [#157](https://github.com/cgj8702-org/hannibal-hub-agents/issues/157)  
> **Author:** Antigravity & Pair Programming Partner  
> **Target Framework:** Google ADK (Agent Development Kit) 2.0+ / `google-agents-cli`  

---

## 1. Executive Summary

As part of the active backlog tracked in Issue [#157](https://github.com/cgj8702-org/hannibal-hub-agents/issues/157), we executed an architectural experiment scaffolding a clean AI agent using Google's official lifecycle toolchain (`agents-cli` v1.5.0) and systematically evaluated its architecture, state management, serving model, and evaluation harness against the production `webhook_agent` operating in `hannibal-hub-agents`.

### Key Conclusions:
1. **Serving Model Separation (Webhooks vs A2A RPC):**  
   `agents-cli` scaffolds an interactive FastAPI HTTP server exposing **Agent2Agent (A2A) JSON-RPC 2.0** routes (`/a2a/{app_name}`) and dynamic Agent Cards (`.well-known/agent-card.json`). In contrast, `hannibal-hub-agents` is designed for asynchronous GitHub Webhook event ingestion backed by Google Cloud Tasks / PubSub, secret resolution across three independent GCP projects, and a stateful worker pool.
2. **ADK Modernization Confirmation:**  
   The experiment confirmed that our recent migration in `webhook_agent`—from legacy `SequentialAgent` to the modern ADK 2.0+ `Workflow` graph, wrapped in an ADK `App` with `Runner` and custom pruning plugins (`ToolOutputPruningPlugin`, `WebhookHistoryPruningPlugin`)—aligns directly with the core abstractions used by `agents-cli`.
3. **High-Value Adoption Opportunity: The Eval Framework:**  
   The primary architectural gap and highest-leverage mechanic in `agents-cli` is its built-in **LLM-as-judge evaluation harness** (`eval_config.yaml`, `response_quality.py`, and structured eval datasets). Adopting this structure into our test suite would provide automated, deterministic regression scoring for pull request reviews and edge-case classification.

---

## 2. Architectural Comparison Matrix

| Dimension | `agents-cli` Scaffolded Architecture (`adk` template) | Production `webhook_agent` (`hannibal-hub-agents`) |
|:---|:---|:---|
| **Primary Ingress** | FastAPI ASGI app with A2A JSON-RPC endpoints (`/a2a/...`) | GitHub Webhook HTTP endpoint (`/webhook`), verified with HMAC SHA256 |
| **Agent Paradigm** | Single interactive `Agent` with tool callbacks | 3-node ADK `Workflow` (`pr_router` ➔ `code_auditor` ➔ `verdict_agent`) |
| **Runtime Container** | ADK `App(root_agent=...)` mounted in FastAPI lifespan | ADK `App(root_agent=Workflow, plugins=[...])` invoked via synchronous/async `Runner` |
| **Session & Storage** | Pluggable `create_session_service_from_options` (`shared://session`, Vertex AI Agent Engine, In-Memory) | In-Memory Session + Firestore Registry (`firestore_registry.py`) for stateful PR review idempotency |
| **Context & Token Pruning** | Default ADK runtime (relies on model context or Agent Engine) | Active `ToolOutputPruningPlugin` (zeroes stale tool responses) + `WebhookHistoryPruningPlugin` (sliding turn window) |
| **Concurrency / Worker Model** | Standard Uvicorn ASGI event loop with task store | Multi-worker background queue with cancellation token registry (`cancellation.py`) |
| **Resilience & Rate Limiting** | Native `HttpRetryOptions(attempts=3)` on `Gemini` model | Custom `RateLimitedGemini` with token-bucket limiter, backoff jitter, 429 server retry header parsing, and TPM descending cascade |
| **Evaluation Harness** | Native `agents-cli eval run` with `eval_config.yaml` and LLM judge | PyTest test suite (235+ unit & integration tests) testing deterministic tools, but no continuous LLM-graded eval dataset |

---

## 3. Deep-Dive: Anatomy of an `agents-cli` Scaffold

When executing `agents-cli create <project_name> -y -p`, the toolchain generates the following project hierarchy:

```
<project_name>/
├── app/
│   ├── __init__.py
│   ├── agent.py               # Defines root_agent (Agent) and App wrapper
│   ├── fast_api_app.py        # Lifespan Runner initialization & A2A endpoint attachment
│   └── app_utils/
│       ├── __init__.py
│       ├── a2a.py             # Agent Card generation & JSON-RPC 2.0 route mapping
│       └── services.py        # Process-wide session/artifact service registry ("shared://")
├── tests/
│   ├── unit/                  # Standard mock tests
│   ├── integration/           # Server E2E and agent pipeline tests
│   └── eval/                  # Model evaluation suite
│       ├── datasets/          # Golden JSON / JSONL test cases with expected references
│       ├── eval_config.yaml   # Configured metrics (custom LLM judge, turn counts)
│       └── response_quality.py# Thread-local Gemini Client LLM-as-judge grader
├── agents-cli-manifest.yaml   # Project metadata and CLI integration manifest
└── pyproject.toml             # uv package definition
```

### 3.1 The A2A Serving Pattern (`app/fast_api_app.py`)
`agents-cli` standardizes on Google's Agent2Agent (A2A) protocol. During startup, the FastAPI `lifespan` dynamically advertises agent capabilities:

```python
agent_card = await AgentCardBuilder(
    agent=agent,
    capabilities=resolved_capabilities,
    rpc_url=f"{resolved_app_url}{rpc_path}",
    agent_version=resolved_agent_version,
).build()
```

While valuable for multi-agent federation (e.g. Gemini Enterprise or cross-service delegation), `webhook_agent` operates in an asynchronous CI/CD gating loop where webhooks are delivered by GitHub's API. Directly adopting the A2A server structure would introduce unnecessary network overhead and security surface area without providing direct benefit for PR processing.

### 3.2 Evaluation & Quality Grading (`tests/eval/`)
The standout capability of the `agents-cli` ecosystem is its standardized evaluation framework:
- **`eval_config.yaml`**: Configures metric pipelines such as `custom_response_quality` and `agent_turn_count`.
- **`response_quality.py`**: Uses a structured Pydantic `_Verdict` (`score: int`, `explanation: str`) evaluated with `gemini-3.7-flash` at `temperature=0` to deterministically grade agent outputs against golden reference datasets.
- **Dataset Execution**: `agents-cli eval run` executes prompt sets across changes and calculates score deltas before deployment.

---

## 4. Latency, Token Quota & Resilience Comparison

### 4.1 Token Efficiency
- **Scaffolded Agent:** Emits raw tools without context pruning. In multi-turn sessions (such as prolonged discussions on a PR), tool responses (e.g., massive 5,000-line git diffs or file contents) remain unpruned in the session event log.
- **Production `webhook_agent`:** Employs `ToolOutputPruningPlugin`, which reverse-scans session history and zeroes out responses older than 3 turns once reclaimed tokens exceed 2,000. It also enforces `WebhookHistoryPruningPlugin(max_events=12)` to prevent context overflow.

### 4.2 Rate Limiting & Quota Management
- **Scaffolded Agent:** Relies solely on Google GenAI SDK's `HttpRetryOptions`. When encountering free-tier rate limits (such as Gemma's 15k TPM ceiling or Gemini Flash RPM quotas), it retries blindly or crashes.
- **Production `webhook_agent`:** Incorporates `RateLimitedGemini`, which dynamically parses Google GenAI 429 quota exhaustion payloads (`google.genai.errors.ClientError`), extracts quota reset timestamps, enforces token-bucket consumption tracking, and triggers model cascade (`_advance_model_chain`) across the TPM descending chain.

---

## 5. Strategic Recommendations for `hannibal-hub-agents`

1. **Retain Custom Ingress Architecture:**  
   Do not migrate `webhook_agent` to the generic `agents-cli` FastAPI/A2A server template. The current decoupled webhook ingestion, HMAC validation, Firestore deduplication, and worker queue architecture is specifically optimized for GitHub CI/CD reliability and zero-bypass gating.

2. **Adopt the `agents-cli` Eval Pattern for Code Review:**  
   Import the `tests/eval` pattern from `agents-cli`:
   - Create golden review datasets in `tests/eval/datasets/` containing synthetic PR diffs with known security vulnerabilities, styling flaws, and trivial docs changes.
   - Implement an automated LLM-as-judge eval runner (`tests/eval/response_quality.py`) using structured outputs to benchmark prompt revisions and model updates before deploying to production.

3. **Align Tool & Manifest Metadata:**  
   Keep `agents-cli-manifest.yaml` compatibility in mind if `hannibal-hub-agents` is ever published or registered in Google Agent Garden or Agent Platform in the future.

