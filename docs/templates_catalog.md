# Hannibal Hub Agents — Prompt Templates Catalog & Architecture Review

This document provides a comprehensive technical catalog, structural review, and sanitization guidelines for all prompt and pull request templates utilized across the Hannibal Hub agents ecosystem.

## Overview of Templates

| Template File | Purpose | Target Object / Context | Sanitization & Parsing Rules |
| :--- | :--- | :--- | :--- |
| **`pr_template.md`** | Standardized PR description structure | Pull Requests (`/create`, PR creation) | Sanitized by `_sanitize_pr_body()` to strip raw placeholder instructions and headers. |
| **`code_review_template.md`** | Structured code review formatting | Initial PR Reviews (`pull_request.opened`, `/review`) | Enforces scorecard metrics, critical issues, minor suggestions, and auditor confidence. |
| **`sync_review_template.md`** | Incremental review & resolution tracking | PR Updates (`pull_request.synchronize`) | Tracks resolution status of previously requested items against incremental commit diffs. |

---

## Detailed Template Specifications

### 1. PR Description Template (`pr_template.md`)
- **Structure**: Divided into Description (Summary & Motivation/Context), Testing (Test Commands & Validation Results), and Security & Policy Checklist.
- **Sanitization**: `_sanitize_pr_body()` programmatically strips template header markers and unchecked placeholder prompts to prevent prompt leakage and maintain clean PR descriptions.

### 2. Initial Code Review Template (`code_review_template.md`)
- **Structure**: Executive Summary, Action Items (Critical & Suggestions), and Potential Risks & Edge Cases.
- **Verdict Enforcement**: Parsed by `_parse_scorecard_scores()` and `_enforce_verdict()` to enforce strict verdict rules (ANY critical issue -> `REQUEST_CHANGES`, confidence <= 3 -> `COMMENT`).

### 3. Synchronization Review Template (`sync_review_template.md`)
- **Structure**: Synchronization Summary, Resolution Tracker (marking previous items as `[RESOLVED]` or `[UNRESOLVED]`), and New Findings.
- **Commit Delta Tracking**: Compares pre-fetched incremental commit diffs against previous bot reviews.

---

## Maintenance Guidelines
- Avoid hardcoding static text in model system prompts when structured templates exist in `src/webhook_agent/templates/`.
- Ensure all template updates preserve Pydantic schema compatibility for review parsers.
