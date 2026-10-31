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
| **Review Intent & Re-Review** | `/review`, `please review`, `/re-review`, `re-review`, `rereview`, `/force-review`, `review this`, `request review`, `run review`, `audit this`, `code review` | PR Comment / Inline Review Comment | Code Auditor Subagent (`SYSTEM_INSTRUCTION`) | Prefetches incremental & full diff; bypasses duplicate bot review suppression on explicit request; runs clinical audit; updates formal review state. |
| **Deep Reasoning Escalation** | `/audit`, `/deep`, `/reason`, `deep review`, `deep audit`, `thorough review`, `complex review`, `escalate` | PR Comment / Inline Review Comment | Code Auditor Subagent (`gemini-3.8-flash`) | Escalates paid primary model chain to `gemini-3.8-flash` for high-complexity architectural and security audits (see `docs/MODEL_CHAIN.md`). |
| **Conversational Pair Programming** | Any conversational comment, technical question, or `@hannibal-hub-agents` mention (without review intent keywords) | PR Comment / Inline Review Comment / Issue Comment | Conversational Subagent (`CONVERSATIONAL_INSTRUCTION`) | Uses codebase grounding tools (`read_file`, `search_files`, `list_dir`, `get_issue`) to provide context-aware answers; writes back as friendly markdown comment via GitHub Issues/PR API. |
---


## Architectural Details

### 1. Dual-Runner Architecture in `WebhookAgent`
- **`_runner` (`_code_auditor`)**: Configured with strict 1-turn clinical review instructions, precompiled AST dossier, and structured Pydantic response models (`read_file`, `search_files`, `list_dir`, `review`). Only invoked when `_is_formal_review_eligible` evaluates to `True`.
- **`_conversational_runner` (`_conversational_agent`)**: Dedicated conversational subagent with inspection and issue creation tools (`read_file`, `search_files`, `list_dir`, `get_issue`, `create_issue`). Prohibited from outputting raw JSON review schemas; outputs natural GitHub Flavored Markdown.

### 3. Comment Rate Limiting & Safety
- Conversational comments are rate-limited via `_COMMENT_RATE_LIMITER` (sliding window per repository/PR key) to prevent runaway conversational loops.
- Bot identity filters (`_is_bot_event`) prevent recursive feedback loops with other automated services.
