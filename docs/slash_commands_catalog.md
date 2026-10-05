# Hannibal Hub Agents — Slash Command Catalog & Architecture Review

This document provides a comprehensive technical catalog, routing reference, and security/reliability audit of all supported slash commands in the Hannibal Hub agents ecosystem.

## Overview of Slash Commands

| Command | Triggers / Aliases | Target Object | Primary Handler / Tool | Reliability & Security Considerations |
| :--- | :--- | :--- | :--- | :--- |
| **`/review`** | `/review`, `/audit`, `/test`, `/critique`, `please review` | Pull Request / Issue Comment | `_prefetch_pr_diff`, `review()` tool | Prefetches PR diff to avoid prompt bloat. Requires structured verdict & invariant enforcement. |
| **`/create`** | `/create` | Pull Request | `get_pr_diff`, `update_pr_description` | Auto-fills PR descriptions and summaries based on commit history. |

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

### 2. `/create`
- **Purpose**: Generates or updates PR descriptions from commit history.
- **Workflow**:
  1. Prefetches commit history summary via `_prefetch_commit_history`.
  2. Generates comprehensive description.
- **Risk Analysis**: Empty commit messages lead to sparse descriptions.

---

## Recommendations & Next Steps
- Enforce rate limiting on resource-intensive review operations.
- Expand test coverage for edge-case parser inputs.
