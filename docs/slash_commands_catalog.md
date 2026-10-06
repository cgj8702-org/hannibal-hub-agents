# Hannibal Hub Agents — Interaction & Natural Language Architecture Guide

This document outlines the interaction model, event routing, and conversational pair programming capabilities of the Hannibal Hub agents ecosystem.

## 🌟 Natural-Language-First & Automated Lifecycle

Hannibal Hub has evolved beyond rigid slash commands. The agent uses a decoupled dual-engine architecture:

1. **Automated PR Audits**: Formal clinical code reviews run automatically on pull request lifecycle events (`pull_request.opened`, `pull_request.synchronize`, `pull_request.ready_for_review`). No slash commands are required.
2. **Conversational Pair Programming**: When engineers ask questions, discuss architecture, or chit-chat on a PR thread or mention the bot, the conversational runner responds directly with sharp, helpful, and friendly markdown comments.
3. **Intent-Based Review Triggers**: When a comment expresses an intent to run or re-run a review, the agent routes to the formal code review auditor.

---

## Interaction Routing Reference

| Interaction Mode | Triggers & Keywords | Target Object | Active Engine | Behavior & Writeback |
| :--- | :--- | :--- | :--- | :--- |
| **Automated Review** | `pull_request.opened`, `pull_request.synchronize`, `pull_request.ready_for_review` | Pull Request | Code Auditor Subagent (`SYSTEM_INSTRUCTION`) | Prefetches diff and compiler findings; enforces structured verdict (`APPROVE` / `REQUEST_CHANGES`); submits formal PR review. |
| **Review Intent** | `/review`, `please review`, `re-review`, `review this`, `request review`, `run review`, `audit this`, `code review` | PR Comment / Inline Review Comment | Code Auditor Subagent (`SYSTEM_INSTRUCTION`) | Prefetches incremental & full diff; runs clinical audit; updates formal review state. |
| **Conversational Pair Programming** | Any conversational comment, technical question, or `@hannibal-hub-agents` mention (without review intent keywords) | PR Comment / Inline Review Comment / Issue Comment | Conversational Subagent (`CONVERSATIONAL_INSTRUCTION`) | Uses codebase grounding tools (`read_file`, etc.) to provide context-aware answers; writes back as friendly markdown comment via GitHub Issues/PR API. |

---

## Architectural Details

### 1. Dual-Runner Architecture in `WebhookAgent`
- **`_runner` (`_code_auditor`)**: Configured with strict 1-turn clinical review instructions, precompiled AST dossier, and structured Pydantic response models. Only invoked when `_is_formal_review_eligible` evaluates to `True`.
- **`_conversational_runner` (`_conversational_agent`)**: Dedicated conversational subagent with full codebase inspection tools (`read_file`, `get_issue`, `get_commit_diff`, `get_current_time`, `google_search_grounding_tool`). Prohibited from outputting raw JSON review schemas; outputs natural GitHub Flavored Markdown.

### 2. Comment Rate Limiting & Safety
- Conversational comments are rate-limited via `_COMMENT_RATE_LIMITER` (sliding window per repository/PR key) to prevent runaway conversational loops.
- Bot identity filters (`_is_bot_event`) prevent recursive loops with other automated services.
