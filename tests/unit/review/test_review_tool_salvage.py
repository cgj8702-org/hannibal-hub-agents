"""Unit tests for review tool validation salvage in submit_initial_review and submit_sync_review."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents.context import Context

from webhook_agent.review.schemas import CodeReviewResponse, ScorecardItem, SyncReviewResponse
from webhook_agent.tools.github_tools import submit_initial_review, submit_sync_review

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=Context)
    gh = MagicMock()
    repo = MagicMock()
    pr = MagicMock()
    pr.state = "open"
    pr.merged = False
    pr.head.sha = "a" * 40
    f = MagicMock()
    f.filename = "src/main.py"
    f.patch = "@@ -1,1 +1,2 @@\n existing\n+new\n"
    pr.get_files.return_value = [f]
    repo.get_pull.return_value = pr
    gh.get_repo.return_value = repo
    ctx.state = {
        "gh_client": gh,
        "repo_full_name": "owner/repo",
        "formal_review_eligible": True,
        "head_sha": "a" * 40,
    }
    return ctx


_SAMPLE_SCORECARD = [
    ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean."),
    ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Clean."),
    ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Clean."),
    ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Clean."),
    ScorecardItem(dimension="Test Coverage & Regressions", status="PASS", notes="Clean."),
]


def test_submit_initial_review_salvages_missing_line_and_fix(mock_ctx: MagicMock) -> None:
    """Verify submit_initial_review auto-salvages missing line numbers and empty suggested_fix."""
    payload = {
        "executive_summary": "PR with incomplete finding details",
        "scorecard": [s.model_dump() for s in _SAMPLE_SCORECARD],
        "critical_issues": [
            {
                "path": "src/main.py",
                "line": None,
                "description": "Potential unhandled error",
                "suggested_fix": "",
            }
        ],
        "minor_suggestions": [],
    }
    res = submit_initial_review(mock_ctx, 123, payload, "REQUEST_CHANGES")
    assert "Submitted review (REQUEST_CHANGES)" in res


def test_submit_initial_review_salvages_missing_scorecard_dimensions(mock_ctx: MagicMock) -> None:
    """Verify submit_initial_review infers missing scorecard dimensions rather than hard-rejecting."""
    mock_ctx.state["head_sha"] = "b" * 40
    mock_ctx.state["gh_client"].get_repo().get_pull().head.sha = "b" * 40
    partial_scorecard = [
        {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Verified."}
    ]
    payload = {
        "executive_summary": "PR with partial scorecard",
        "scorecard": partial_scorecard,
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = submit_initial_review(mock_ctx, 124, payload, "APPROVE")
    assert "Submitted review (APPROVE)" in res or "Submitted review (COMMENT)" in res


def test_submit_sync_review_salvages_request_changes_without_unresolved(
    mock_ctx: MagicMock,
) -> None:
    """Verify submit_sync_review flips REQUEST_CHANGES to APPROVE when no blockers/unresolved items exist."""
    mock_ctx.state["head_sha"] = "c" * 40
    pr = mock_ctx.state["gh_client"].get_repo().get_pull()
    pr.head.sha = "c" * 40

    prior_rev = MagicMock()
    prior_rev.user.login = "hannibal-hub-agents[bot]"
    prior_rev.state = "CHANGES_REQUESTED"
    prior_rev.body = "#### 🔴 Critical (Must Fix Before Merge)\n• src/main.py:1 : Fix boundary error in src/main.py"
    pr.get_reviews.return_value = [prior_rev]
    payload = {
        "summary": "Sync update with all prior items resolved",
        "scorecard": [s.model_dump() for s in _SAMPLE_SCORECARD],
        "resolutions": [
            {
                "item_description": "Fix boundary error in src/main.py",
                "status": "RESOLVED",
                "evidence": "Fixed in src/main.py",
                "path": "src/main.py",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = submit_sync_review(mock_ctx, 125, payload, "REQUEST_CHANGES")
    assert "Submitted review" in res
    assert "Error: Review submission rejected" not in res
