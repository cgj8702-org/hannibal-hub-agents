"""Unit tests for repository-aware and commit-aware review dismissal logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.review.review_enforcer import (
    _submit_formal_review,
    is_repo_with_native_stale_review_dismissal,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _make_mock_pr(head_sha: str = "head-1", reviews: list[MagicMock] | None = None) -> MagicMock:
    pr = MagicMock()
    pr.state = "open"
    pr.merged = False
    pr.head.sha = head_sha
    pr.get_reviews.return_value = list(reviews or [])
    new_review = MagicMock()
    new_review.id = 999
    new_review.html_url = "https://github.com/org/repo/pull/1#review-999"
    pr.create_review.return_value = new_review
    return pr


def _make_bot_review(review_id: int, commit_id: str, state: str = "APPROVED") -> MagicMock:
    rv = MagicMock()
    rv.id = review_id
    rv.user.login = "hannibal-hub-agents[bot]"
    rv.commit_id = commit_id
    rv.state = state
    return rv


def test_is_repo_with_native_stale_review_dismissal_defaults() -> None:
    """Verify default repo matching against DEFAULT_NATIVE_STALE_REVIEW_DISMISSAL_REPOS."""
    assert is_repo_with_native_stale_review_dismissal("cgj8702-org/hannibal-hub-agents") is True
    assert is_repo_with_native_stale_review_dismissal("hannibal-hub-agents") is True
    assert is_repo_with_native_stale_review_dismissal("CGJ8702-ORG/HANNIBAL-HUB-AGENTS") is True

    # Other repos must not match
    assert is_repo_with_native_stale_review_dismissal("cgj8702-org/adk-agent") is False
    assert is_repo_with_native_stale_review_dismissal("adk-agent") is False
    assert is_repo_with_native_stale_review_dismissal("other/repo") is False
    assert is_repo_with_native_stale_review_dismissal("") is False


def test_is_repo_with_native_stale_review_dismissal_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify NATIVE_STALE_REVIEW_DISMISSAL_REPOS env override."""
    monkeypatch.setenv(
        "NATIVE_STALE_REVIEW_DISMISSAL_REPOS",
        "custom-org/custom-repo, another-repo",
    )
    assert is_repo_with_native_stale_review_dismissal("custom-org/custom-repo") is True
    assert is_repo_with_native_stale_review_dismissal("another-repo") is True
    assert is_repo_with_native_stale_review_dismissal("cgj8702-org/hannibal-hub-agents") is False


def test_dismissal_skipped_for_repo_with_native_dismissal_changes_requested() -> None:
    """Repos with native stale review dismissal skip manual dismissal even for CHANGES_REQUESTED."""
    prior = _make_bot_review(101, commit_id="old-sha", state="CHANGES_REQUESTED")
    pr = _make_mock_pr(head_sha="head-1", reviews=[prior])
    body = '{"executive_summary":"Approved new commit.","critical_issues":[]}'

    status, submitted = _submit_formal_review(
        pr,
        body,
        "APPROVE",
        "cgj8702-org/hannibal-hub-agents#101",
        {"review_mode": "sync"},
        analyzed_sha="head-1",
    )

    assert submitted is True
    assert "Submitted review (APPROVE)" in status
    prior.dismiss.assert_not_called()


def test_dismissal_skipped_for_repo_with_native_dismissal_approved() -> None:
    """Repos with native stale review dismissal skip manual dismissal for APPROVED reviews."""
    prior = _make_bot_review(102, commit_id="old-sha", state="APPROVED")
    pr = _make_mock_pr(head_sha="head-1", reviews=[prior])
    body = '{"executive_summary":"Approved new commit.","critical_issues":[]}'

    status, submitted = _submit_formal_review(
        pr,
        body,
        "APPROVE",
        "cgj8702-org/hannibal-hub-agents#102",
        {"review_mode": "sync"},
        analyzed_sha="head-1",
    )

    assert submitted is True
    assert "Submitted review (APPROVE)" in status
    prior.dismiss.assert_not_called()


def test_other_repo_dismisses_prior_changes_requested() -> None:
    """Non-native repos must dismiss CHANGES_REQUESTED reviews to clear the merge blocker."""
    prior = _make_bot_review(101, commit_id="5be1147", state="CHANGES_REQUESTED")
    pr = _make_mock_pr(head_sha="5be1147", reviews=[prior])
    body = '{"executive_summary":"Resolved previous issues.","critical_issues":[]}'

    status, submitted = _submit_formal_review(
        pr,
        body,
        "APPROVE",
        "cgj8702-org/adk-agent#2",
        {"review_mode": "sync"},
        analyzed_sha="5be1147",
        force=True,
    )

    assert submitted is True
    assert "Submitted review (APPROVE)" in status
    prior.dismiss.assert_called_once_with("Superseded by fresh code review on latest commit.")


def test_other_repo_dismisses_stale_approved_review_when_commit_moves() -> None:
    """Non-native repos must dismiss APPROVED reviews when the PR moves to a new commit."""
    prior = _make_bot_review(101, commit_id="old-head-111", state="APPROVED")
    pr = _make_mock_pr(head_sha="new-head-222", reviews=[prior])
    body = '{"executive_summary":"Reviewed new commit.","critical_issues":[]}'

    status, submitted = _submit_formal_review(
        pr,
        body,
        "APPROVE",
        "cgj8702-org/adk-agent#2",
        {"review_mode": "sync"},
        analyzed_sha="new-head-222",
    )

    assert submitted is True
    assert "Submitted review (APPROVE)" in status
    prior.dismiss.assert_called_once_with("Superseded by fresh code review on latest commit.")


def test_other_repo_preserves_approved_review_on_same_commit_rereview() -> None:
    """Non-native repos must NOT dismiss an APPROVED review during a /re-review of the same commit."""
    prior = _make_bot_review(101, commit_id="5be1147", state="APPROVED")
    pr = _make_mock_pr(head_sha="5be1147", reviews=[prior])
    body = '{"executive_summary":"Re-review of current commit.","critical_issues":[]}'

    status, submitted = _submit_formal_review(
        pr,
        body,
        "APPROVE",
        "cgj8702-org/adk-agent#2",
        {"review_mode": "sync"},
        analyzed_sha="5be1147",
        force=True,
    )

    assert submitted is True
    assert "Submitted review (APPROVE)" in status
    # Prior approval on the same commit must NOT be dismissed!
    prior.dismiss.assert_not_called()
