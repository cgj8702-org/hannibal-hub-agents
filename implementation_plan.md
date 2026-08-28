# 🏛️ Implementation Plan: Hannibal Hub Agents Roadmap & Issue #104 Refactor

Overhaul `implementation_plan.md` to remove hallucinated abstractions and ground all active engineering tasks strictly in **Issue #104** and current repository state.

---

## 🍵 User Review Required

> [!IMPORTANT]
> **Focus Areas**: The plan addresses the 7 remaining items on **Issue #104**, categorized into **Logging Hygiene**, **ADK Graph & Proactive Agent Architecture**, and **Tools & Governance**.

> [!TIP]
> **Execution Strategy**: We will implement these items in logical phases, starting with high-impact logging hygiene and progressing to ADK graph workflow evaluation and search tool guardrails.

---

## ❓ Open Questions

1. **ADK Graph Scope**: Would you prefer evaluating ADK Graph workflows on the webhook routing pipeline (`processor.py`) or the auto-fix pipeline (`auto_fix_feedback.py`) first?
2. **Search Tool Model Assignment**: Google Search grounding is supported on `gemini-3.5-flash-lite`. Should search queries be restricted to specific slash commands (e.g. `/search` or `/research`)?

---

## 🛠️ Proposed Changes

### Phase 1: Logging Hygiene & Telemetry Alignment

#### [MODIFY] [processor.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/processor.py)
- **Named Loggers**: Transition from generic logger to `logging.getLogger("webhook_agent.processor")`.
- **Code Truncation**: Truncate raw code diffs and event payloads in log output to max 300 characters to prevent Cloud Logging quota bloat.
- **Severity Accuracy**: Audit all `logger.warning` / `logger.info` calls to ensure non-error events (e.g. skipped duplicate deliveries or non-PR comments) use `DEBUG` or `INFO`.

#### [MODIFY] [worker.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/worker.py)
- **Named Loggers**: Use `logging.getLogger("webhook_agent.worker")`.
- **Accurate Severity**: Ensure Pub/Sub polling timeouts and heartbeat logs use `DEBUG` level.

#### [MODIFY] [webhook_agent.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/webhook_agent.py)
- **Named Loggers**: Use `logging.getLogger("webhook_agent.agent")`.
- **Response Truncation in Logs**: Truncate LLM thought traces and multi-line responses in system logs.

---

### Phase 2: ADK Graph Workflow & Proactive Agent Architecture

#### [MODIFY] [agent_core.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/agent_core.py)
- **ADK Graph Workflow**: Evaluate replacing `SequentialAgent` with ADK Graph definitions if graph representation improves human developer readability of agent execution paths.
- **Proactive Execution Triggers**: Investigate background scheduled evaluation loops for pending PR checks.

---

### Phase 3: Tools & Governance

#### [NEW] [search_tool.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/tools/search_tool.py)
- **Google Search Grounding**: Re-implement `google_search` tool for `gemini-3.5-flash-lite` with strict programmatic guardrails (max 3 searches per request, query sanitization, mandatory URL citation format).

#### [MODIFY] [webhook_agent.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/webhook_agent.py)
- **Autonomous PR Template Requirement**: Enforce `_fetch_repo_pr_template()` whenever the agent autonomously creates PRs, ensuring full checklist compliance.

---

## 🧪 Verification Plan

### Automated Tests
- Run `bash scripts/ruff-all.sh` to ensure zero linter errors.
- Run `uv run pytest` to ensure all 166+ unit tests pass.
- Add unit tests for log payload truncation and `search_tool` programmatic guardrails.

### Manual Verification
- Execute test webhook events and inspect Cloud Logging via `gcloud logging read` to verify clean log formatting and accurate severities.
