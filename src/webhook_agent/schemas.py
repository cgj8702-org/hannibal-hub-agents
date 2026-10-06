"""Transitional backwards-compatibility shim for schemas.

Relocated to webhook_agent.review.schemas in Phase 6b modularization.
"""

from __future__ import annotations

from webhook_agent.review.schemas import (
    BREAKING_RISK_KEYWORDS,
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    SyncResolutionItem,
    SyncReviewResponse,
    VerifiedInvariant,
    clean_field_string,
    has_genuine_summary_risk,
    is_implausible_body,
    is_not_cheap_finding,
)

__all__ = [
    "BREAKING_RISK_KEYWORDS",
    "CodeReviewResponse",
    "IssueItem",
    "RiskItem",
    "SyncResolutionItem",
    "SyncReviewResponse",
    "VerifiedInvariant",
    "clean_field_string",
    "has_genuine_summary_risk",
    "is_implausible_body",
    "is_not_cheap_finding",
]
