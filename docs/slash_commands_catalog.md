# Hannibal Hub Agents — Slash Command Catalog & Architecture Review

This document provides a comprehensive technical catalog, routing reference, and security/reliability audit of all supported slash commands in the Hannibal Hub agents ecosystem.

## Overview of Slash Commands

| Command | Triggers / Aliases | Target Object | Primary Handler / Tool | Reliability & Security Considerations |
| :--- | :--- | :--- | :--- | :--- |
| **`/review`** | `/review`, `/audit`, `/test`, `/critique`, `please review` | Pull Request / Issue Comment | `_prefetch_pr_diff`, `review()` tool | Prefetches PR diff to avoid prompt bloat. Requires confidence & verdict scorecard enforcement. |
| **`/fix`** | `/fix`, `/auto`, `/fix-it` | Pull Request | `auto_fix_pr_review_feedback` | Executes in isolated Git Worktree, runs ruff and pytest verification before pushing. |
| **`/resolve`** | `/resolve` | Pull Request | `resolve_merge_conflicts` | Uses ephemeral worktree and Gemini generative code block synthesis for merge conflict resolution. |
| **`/create`** | `/create` | Pull Request | `get_pr_diff`, `update_pr_description` | Auto-fills PR descriptions and summaries based on commit history. |
| **`/implement`** | `/implement`, `/feature` | Issue / Issue Comment | `AgentCore` / Agent execution | Extracts instruction and initiates autonomous feature implementation workflow. |

---

## Detailed Architectural Review

### 1. `/review` & Review Aliases
- **Purpose**: Initiates a formal code review on a pull request.
- **Workflow**: 
  1. Comment payload detected by `_should_prefetch_diff`.
  2. `_prefetch_pr_diff` fetches repository files and patches via PyGitHub.
  3. Context injected into `raw_payload["pr_diff"]`.
  4. Agent parses diff and evaluates across 4 mandatory audit dimensions.
- **Risk Analysis**: Large PRs (>500 lines) can cause prompt bloat or token limit saturation. Mitigation: Prefetching formats patches concisely.

### 2. `/fix` / `/auto` / `/fix-it`
- **Purpose**: Automatically resolves code review feedback and test failures.
- **Workflow**:
  1. Triggered via comment on PR.
  2. Creates isolated Git Worktree.
  3. Applies Gemini-synthesized code patches.
  4. Runs linters (`ruff`) and test suite (`pytest`).
  5. Commits and pushes back to origin.
- **Risk Analysis**: Potential regression if tests are insufficient. Mitigation: Strict verification via `pytest` and `ruff` before push.

### 3. `/resolve`
- **Purpose**: Resolves merge conflicts on out-of-date pull requests.
- **Workflow**:
  1. Pre-executed via `_preexecute_resolve_command` when `/resolve` is present.
  2. Spawns isolated Git Worktree.
  3. Synthesizes conflict resolution patches.
  4. Verifies via test suite and pushes.
- **Risk Analysis**: Complex multi-file merge conflicts. Mitigation: Isolated worktree prevents pollution of working directory.

### 4. `/create`
- **Purpose**: Generates or updates PR descriptions from commit history.
- **Workflow**:
  1. Prefetches commit history summary via `_prefetch_commit_history`.
  2. Generates comprehensive description.
- **Risk Analysis**: Empty commit messages lead to sparse descriptions.

### 5. `/implement` / `/feature`
- **Purpose**: Triggers end-to-end feature implementation from issue descriptions.
- **Workflow**:
  1. `_preexecute_implement_command` extracts instruction text.
  2. Delegated to `AgentCore` execution loop.

---

## Recommendations & Next Steps
- Enforce rate limiting on resource-intensive commands (`/fix`, `/resolve`).
- Expand test coverage for edge-case parser inputs.
