# 🏛️ Implementation Plan: Refactoring Webhook Auditor with Google ADK Primitives & ADK-Samples SDLC Agents

Refactor the Webhook Auditor agent (`hannibal-hub-agents`) from a single-prompt monolithic review listener into a **modular, multi-agent ADK pipeline** grounded in software development agent patterns from `adk-samples` (`llm-auditor`, `sdlc-technical-designer`, `sdlc-task-planner`, `software-bug-assistant`, `safety-plugins`, and `post_review_comments.py`). 

This eliminates prompt leakage, prevents forced risk hallucinations on clean PRs, enforces diff-grounded AST risk auditing, adds line-anchor validation, enables Gemini Thinking Mode for deep code reasoning, adds cross-session repository memory, and establishes continuous evaluation via `agents-cli eval`.

---

## 🍵 User Review Required

> [!IMPORTANT]
> **Non-Breaking API Contract**: This refactor maintains full backward compatibility with existing GitHub webhook payloads and output formats while upgrading the internal agent execution engine to Google ADK primitives.

> [!TIP]
> **Zero Forced Risk Hallucinations**: By leveraging Pydantic structured output (`output_schema`), clean dev/docs PRs can now return `risks: []` without violating system instructions.

> [!NOTE]
> **ADK-Samples Integration**: Adapts patterns directly from `adk-samples` SDLC agents:
> 1. **`llm-auditor`**: Multi-agent `SequentialAgent` sub-agent pipeline.
> 2. **`sdlc-technical-designer`**: `BuiltInPlanner` with `ThinkingConfig(include_thoughts=True, thinking_budget=-1)` for deep code analysis.
> 3. **`post_review_comments.py`**: Diff line-anchoring validation to prune out-of-diff comments before atomic GitHub review posting.
> 4. **`safety-plugins`**: `AgentAsAJudge` guardrail plugin to verify comment tone and prevent secret leaks.
> 5. **`cross-session-memory`**: Session state tracking for repository context and past PR review history.

---

## ❓ Open Questions

None. The proposed ADK primitives (`SequentialAgent`, `output_schema`, `BasePlugin`, `agents-cli eval`) and `adk-samples` software dev agent patterns match the existing Google ADK setup in `hannibal-hub-agents`.

---

## 🛠️ Proposed Changes

### Core Agent Architecture & Pipeline

#### [NEW] [audit_schema.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/audit_schema.py)

- Define Pydantic models for structured output:
  - `RiskItem`: `category` (concurrency, memory, security, breaking_change, none), `file`, `line_range`, `description`, `remediation`.
  - `AuditVerdict`: `verdict` (APPROVE, REQUEST_CHANGES, COMMENT), `confidence` (0.0–5.0), `pr_type` (dev_docs, minor_fix, core_backend), `summary`, `risks` (List[RiskItem] defaulting to `[]`).

#### [MODIFY] [webhook_agent.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/webhook_agent.py)

- Replace single LLM prompt call with an ADK `SequentialAgent` pipeline (modeled after `adk-samples/python/agents/llm-auditor` and `sdlc-technical-designer`):
  1. `pr_router` agent: Inspects modified file paths and classifies PR scope (`dev_docs`, `minor_fix`, `core_backend`).
  2. `code_auditor` agent: Equipped with `BuiltInPlanner` thinking mode (`thinking_budget=-1`), executing diff-grounded AST and risk analysis (fast-pathed for pure dev/docs PRs).
  3. `verdict_agent`: Enforces `output_schema=AuditVerdict` to generate structured JSON verdict in a single model turn.

---

### Sanitization & Safety Guardrails (Inspired by `safety-plugins`)

#### [NEW] [sanitizer_plugin.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/sanitizer_plugin.py)

- Implement `PromptSanitizerPlugin(BasePlugin)` extending `after_model_callback`.
- Intercept and strip meta-prompt blockquotes or system instruction text (e.g. `Finding zero risks...`, `> [!IMPORTANT]`) before rendering the final GitHub PR comment.
- Add PII/Secret leakage guardrails to ensure API keys or connection strings are never echoed into PR comments.

---

### Diff-Grounding, Line Anchoring & Posting (Inspired by `adk-samples`)

#### [MODIFY] [diff_tools.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/diff_tools.py)

- Add FunctionTools:
  - `get_pr_diff_file_map`: Returns AST changes per file.
  - `verify_line_reference`: Validates that cited risk lines exist within modified diff chunks.

#### [NEW] [comment_poster.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/comment_poster.py)

- Implement review comment poster with line-anchoring validation (adapted from `adk-samples/.github/scripts/post_review_comments.py`):
  - Parse `AuditVerdict` JSON findings.
  - Recompute line-position anchors against the PR diff hunk header to verify cited lines exist in modified chunks.
  - Prune invalid/unanchorable line comments individually so an LLM line hallucination never causes GitHub to reject the entire PR review payload.
  - Post formatted review payload via GitHub API in a single atomic request (eliminating high-token tool loop round-trips).

---

### Repository Memory & Context Persistence (Phase 2 Upgrade)

#### [NEW] [repo_memory.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/repo_memory.py)

- Implement cross-session memory service using ADK `FirestoreMemoryService` / session state (modeled after `adk-samples/core/python/cross-session-memory`):
  - Stores past PR review verdicts, architectural patterns, and recurring anti-patterns per repository.
  - Enables the agent to reference historical PR context when reviewing new code changes.

---

### Observability & Logging Architecture

- Use standard **GCP Cloud Logging / Structured JSON Logging** (via Python's `logging` module and `google-cloud-logging`) for lightweight, zero-dependency telemetry (token counts, execution latency, and trace context) without requiring BigQuery or external database setup.

---

### Quality Evaluation Suite

#### [NEW] [tests/eval/eval_config.yaml](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/tests/eval/eval_config.yaml)

- Configure `agents-cli eval` metrics (`zero_hallucinated_risks`, `diff_grounding_accuracy`, `prompt_purity`).

#### [NEW] [tests/eval/datasets/pr_reviews.json](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/tests/eval/datasets/pr_reviews.json)

- Create evaluation dataset containing historical clean PR diffs (expecting `risks: []`) and buggy PR diffs (expecting targeted `REQUEST_CHANGES`).

---

## 🧪 Verification Plan

### Automated Tests
- Run `./scripts/ruff-all.sh` to ensure zero linter errors across all modified and new files.
- Run `uv run python -m pytest tests/unit/` to verify unit test suite pass rate.
- Run `agents-cli eval run` to evaluate the new pipeline against historical PR datasets.

### Manual Verification
- Trigger local webhook event with synthetic dev/docs PR diff ➔ Confirm GitHub comment output is clean, has zero prompt leakage, and contains `risks: []`.
- Test comment payload poster with synthetic out-of-diff line numbers ➔ Verify poster safely prunes invalid anchors without failing overall review payload.
