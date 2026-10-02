"""Unit tests for SyncReviewResponse and CodeReviewResponse normalization without summary risk scraping."""

from __future__ import annotations

import pytest

from webhook_agent.formatter import calculate_sync_verdict
from webhook_agent.schemas import (
    CodeReviewResponse,
    SyncReviewResponse,
    has_genuine_summary_risk,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_has_genuine_summary_risk_disabled() -> None:
    """Verify has_genuine_summary_risk returns False and does not trip on resolution or risk phrases."""
    summary = (
        "Verified that the uv.lock updates correctly reflect the upgrade of mypy from 2.0.0 to 2.3.1 "
        "along with its standard transitive dependency resolution updates. Previous concerns regarding "
        "unrelated packages and environment markers have been evaluated and resolved as standard resolver behavior."
    )
    assert has_genuine_summary_risk(summary) is False
    assert has_genuine_summary_risk("lockfile corruption was resolved") is False
    assert has_genuine_summary_risk(None) is False


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_sync_review_response_positive_summary_no_synthetic_critical() -> None:
    """Ensure a sync review with a positive summary and all RESOLVED items produces 0 critical issues and APPROVE."""
    data = {
        "summary": (
            "Verified that the uv.lock updates correctly reflect the upgrade of mypy from 2.0.0 to 2.3.1 "
            "along with its standard transitive dependency resolution updates. Previous concerns regarding "
            "unrelated packages and environment markers have been evaluated and resolved as standard resolver behavior."
        ),
        "resolutions": [
            {
                "item_description": "Verified that jinxed / ansicon environment marker adjustment is not applicable.",
                "status": "RESOLVED",
                "evidence": "Verified in commit diff.",
            },
            {
                "item_description": "Confirmed that updates to transitive dependencies are standard resolver updates.",
                "status": "RESOLVED",
                "evidence": "Verified in commit diff.",
            },
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }

    sync_obj = SyncReviewResponse.model_validate(data)
    assert len(sync_obj.critical_issues) == 0
    verdict = calculate_sync_verdict(sync_obj)
    assert verdict == "APPROVE"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_sync_review_response_unresolved_critical_retains_request_changes() -> None:
    """Ensure an unresolved critical issue still yields REQUEST_CHANGES."""
    data = {
        "summary": "Re-review detected remaining issues.",
        "resolutions": [
            {
                "item_description": "Fix SQL injection vulnerability in search endpoint.",
                "status": "UNRESOLVED",
                "evidence": "Unresolved in commit diff.",
                "category": "CRITICAL",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }

    sync_obj = SyncReviewResponse.model_validate(data)
    verdict = calculate_sync_verdict(sync_obj)
    assert verdict == "REQUEST_CHANGES"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_code_review_response_no_synthetic_critical_from_summary() -> None:
    """Ensure CodeReviewResponse does not synthesize a critical issue from an executive summary mentioning breaking changes."""
    data = {
        "executive_summary": "Evaluated breaking change risks and confirmed no breaking change occurred.",
        "critical_issues": [],
        "minor_suggestions": [],
        "risks_and_edge_cases": [],
    }

    cr_obj = CodeReviewResponse.model_validate(data)
    assert len(cr_obj.critical_issues) == 0
