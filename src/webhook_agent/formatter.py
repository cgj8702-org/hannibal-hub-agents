"""Transitional backwards-compatibility shim for formatter.

Relocated to webhook_agent.review.formatter in Phase 6b modularization.
"""

from __future__ import annotations

from webhook_agent.review.formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    extract_json_payload,
    logger,
    normalize_code_review_dict,
    normalize_sync_review_dict,
    parse_text_review_to_dict,
    render_code_review_markdown,
    render_sync_review_markdown,
    truncate_log_payload,
)

__all__ = [
    "calculate_strict_verdict",
    "calculate_sync_verdict",
    "extract_json_payload",
    "logger",
    "normalize_code_review_dict",
    "normalize_sync_review_dict",
    "parse_text_review_to_dict",
    "render_code_review_markdown",
    "render_sync_review_markdown",
    "truncate_log_payload",
]
