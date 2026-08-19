# 🏛️ Implementation Plan: High-Trust, Deep Architectural Code Review & Output System

Redesign the Webhook Auditor agent's output and template system in `hannibal-hub-agents` to deliver **deeply reasoned, highly trustworthy, and evidence-grounded code reviews**. This eliminates generic cheerleading, enforces strict AST diff line-anchoring, leverages Gemini Thinking Mode for thorough architectural auditing, and structures reviews into clear, high-signal Markdown.

---

## 🍵 User Review Required

> [!IMPORTANT]
> **High-Trust Review Principle**: Reviews MUST be grounded strictly in empirical diff evidence. Every cited finding MUST include exact file paths, line citations (`L45-L50`), specific failure mechanisms, and concrete remediation code snippets.

> [!TIP]
> **Anti-Sycophancy & High Signal**: Generic praise ("Great refactoring!", "Rock-solid PR!") is strictly prohibited. The auditor output focuses purely on objective technical analysis, edge cases, and actionable code improvements.

---

## ❓ Open Questions

None. The proposed structure optimizes review trust, depth, and clarity across all PR scopes.

---

## 🛠️ Proposed Changes

### High-Trust Audit Instructions & Rubric

#### [MODIFY] [webhook_agent.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/webhook_agent.py)

- Refine system instructions for `code_auditor` sub-agent:
  - Enforce 4 mandatory audit dimensions:
    1. **Logic & Boundaries**: Off-by-one errors, null/None dereferences, unhandled exceptions, resource leaks.
    2. **Concurrency & Memory**: Async race conditions, shared state mutation without locks, memory growth.
    3. **Security & Secrets**: Hardcoded secrets, input sanitization, authentication/authorization boundaries.
    4. **Contract Integrity**: Breaking signature changes, missing invocation site updates across the codebase.
  - Require explicit evidence-based justification for every verdict determination.

---

### High-Signal Output Templates (`src/webhook_agent/templates/`)

#### [MODIFY] [code_review_template.md](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/templates/code_review_template.md)

- Redesign initial code review format for maximum readability and trust:
  - **Verdict Header**: Verdict (`APPROVE`, `REQUEST_CHANGES`, `COMMENT`), Auditor Confidence (`0.0-5.0`), PR Scope (`dev_docs`, `minor_fix`, `core_backend`).
  - **Executive Summary**: 1-2 sentences on architectural intent and verdict rationale.
  - **Critical Blocking Issues**: 🔴 Mandatory file:line citation, failure mechanism, and code remediation block for any `REQUEST_CHANGES` verdict.
  - **Diff-Anchored Risk Analysis**: 🟡 Edge cases, concurrency boundaries, or security considerations with line ranges.
  - **Minor Maintainability Notes**: Actionable refactoring suggestions.

#### [MODIFY] [sync_review_template.md](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/templates/sync_review_template.md)

- Redesign incremental re-review (`synchronize`) template:
  - **Commit Delta Summary**: `before_sha` ➔ `head_sha` incremental changes.
  - **Resolution Tracker**: Item-by-item verification (`RESOLVED` / `UNRESOLVED`) with line citations.
  - **Verdict Transition**: Clear transition status (`REQUEST_CHANGES` ➔ `APPROVE`).

---

### Structured Formatting & Line-Anchored Poster

#### [MODIFY] [comment_poster.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/comment_poster.py)

- Update `render_review_markdown` to generate the new high-trust Markdown review layout.
- Ensure all line references are verified against diff hunk headers using `verify_line_reference`.

#### [MODIFY] [formatter.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/formatter.py)

- Harmonize markdown rendering methods to produce the updated high-trust review format across all webhook events.

---

## 🧪 Verification Plan

### Automated Tests
- Run `./scripts/ruff-all.sh` to ensure zero linter errors.
- Run `uv run python -m pytest tests/unit/` to verify unit test suite pass rate.
- Add unit tests in `tests/unit/webhook_agent/test_output_system.py` verifying high-trust markdown structure generation.

### Manual Verification
- Render synthetic `AuditVerdict` payloads and inspect generated Markdown outputs to verify formatting elegance, line citation clarity, and technical depth.
