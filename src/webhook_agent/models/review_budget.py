"""PR review comment budget, round decay, and convergence engine.

Re-exported from webhook_agent.review.review_budget for backwards compatibility.
"""

from __future__ import annotations

from webhook_agent.review.review_budget import (
    DEFAULT_DECAY,
    DEFAULT_LIFETIME_CAP,
    DEFAULT_MIN_ALLOWANCE,
    ReviewHistorySummary,
    compute_round_allowance,
    should_suppress_round,
    summarise_review_history,
)

__all__ = [
    "DEFAULT_DECAY",
    "DEFAULT_LIFETIME_CAP",
    "DEFAULT_MIN_ALLOWANCE",
    "ReviewHistorySummary",
    "compute_round_allowance",
    "should_suppress_round",
    "summarise_review_history",
]
