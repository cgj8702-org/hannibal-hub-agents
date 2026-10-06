# Hannibal Hub Agents — Prompt Templates Catalog & Architecture Review

This document provides a technical overview of review rendering in Hannibal Hub Agents.

## Review Formatting Architecture

Following the Zero-Bypass Architecture migration and PR #205 tool pruning, code reviews and PR synchronization reviews are deterministically rendered via Pydantic schemas in `src/webhook_agent/formatter.py` (`CodeReviewResponse` and `SyncReviewResponse`).

The local markdown template files (`pr_template.md`, `code_review_template.md`, `sync_review_template.md`) and prototype schema `audit_schema.py` have been purged in favor of strict Pydantic model validation and structured output formatting.

## Maintenance Guidelines
- Ensure all review outputs conform to the Pydantic schemas defined in `src/webhook_agent/schemas.py`.
- Final GitHub Markdown review comments and summaries are rendered deterministically by `formatter.py`.
