"""Transitional backwards-compatibility shim for pr_context.

Relocated to webhook_agent.github.pr_context in Phase 6c modularization.
"""

from __future__ import annotations

from webhook_agent.github.pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
    logger,
)

__all__ = [
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_should_prefetch_diff",
    "logger",
]
