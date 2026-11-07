"""Deterministic Hunter Package for high-recall candidate issue discovery and budgeting.

Exposes:
- HunterEngine: Main orchestrator for parsing candidate findings with 3+ grouping.
- ReviewBudgeter: Churn pruner, comment budgeter, and source-test pairing engine.
- DefectGrouper: 3+ defect grouping engine collapsing repeated issues into single comments.
"""

from __future__ import annotations

from webhook_agent.review.hunter.budgeter import (
    BUDGET_TABLE,
    BULK_DATA_CHURN,
    BULK_DATA_EXT,
    SKIP_PATTERNS,
    ReviewBudgeter,
)
from webhook_agent.review.hunter.grouping import DEFECT_CATEGORIES, DefectGrouper
from webhook_agent.review.hunter.hunter_engine import HunterEngine

__all__ = [
    "BUDGET_TABLE",
    "BULK_DATA_CHURN",
    "BULK_DATA_EXT",
    "DEFECT_CATEGORIES",
    "SKIP_PATTERNS",
    "DefectGrouper",
    "HunterEngine",
    "ReviewBudgeter",
]
