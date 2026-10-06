"""Transitional backwards-compatibility shim for proactive_service.

Relocated to webhook_agent.review.proactive_service in Phase 6b modularization.
"""

from __future__ import annotations

from webhook_agent.review.proactive_service import (
    ProactiveEvaluator,
    build_reconciliation_cache_key,
    clear_reconciliation_cache,
    is_reconciliation_claimed,
    logger,
    release_reconciliation_claim,
    try_claim_reconciliation,
)

__all__ = [
    "ProactiveEvaluator",
    "build_reconciliation_cache_key",
    "clear_reconciliation_cache",
    "is_reconciliation_claimed",
    "logger",
    "release_reconciliation_claim",
    "try_claim_reconciliation",
]
