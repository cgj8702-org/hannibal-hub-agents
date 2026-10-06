"""Transitional backwards-compatibility shim for fast_path.

Relocated to webhook_agent.review.fast_path in Phase 6b modularization.
"""

from __future__ import annotations

from webhook_agent.review.fast_path import (
    _submit_formal_review,
    evaluate_dependency_fast_path,
    is_base_branch_merge_sync,
    logger,
)

__all__ = [
    "_submit_formal_review",
    "evaluate_dependency_fast_path",
    "is_base_branch_merge_sync",
    "logger",
]
