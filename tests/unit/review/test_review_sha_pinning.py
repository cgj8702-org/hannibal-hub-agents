"""Unit tests for pinning formal reviews to the commit that was actually analysed."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.review.metadata import (
    extract_review_metadata,
    stamp_review_metadata,
)
from webhook_agent.review.review_enforcer import _submit_formal_review

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

ANALYSED = "a" * 40
MOVED_TO = "b" * 40


def _pr(head_sha: str, reviews: list[Any] | None = None) -> MagicMock:
    pr = MagicMock()
    pr.head.sha = head_sha
    pr.get_reviews.return_value = list(reviews or [])
    return pr


def _bot_review(commit_id: str) -> MagicMock:
    review = MagicMock()
    review.user.login = "hannibal-hub-agents[bot]"
    review.commit_id = commit_id
    review.state = "COMMENTED"
    review.id = 99
    return review


def test_defers_when_head_moved_during_audit() -> None:
    """A review must never be stamped onto a commit the audit never saw."""
    pr = _pr(MOVED_TO)

    with (
        patch("webhook_agent.review.review_enforcer._enforce_verdict") as enforce,
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        message, submitted = _submit_formal_review(
            pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED
        )

    assert submitted is False
    assert message.startswith("Deferred:")
    assert ANALYSED[:7] in message
    assert MOVED_TO[:7] in message
    pr.create_review.assert_not_called()
    enforce.assert_not_called()
    registry.claim.assert_not_called()


def test_submits_pinned_to_analysed_commit_with_metadata() -> None:
    """On a matching head, the review is pinned via commit= and stamped with the SHA."""
    pr = _pr(ANALYSED)

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        message, submitted = _submit_formal_review(
            pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED
        )

    assert submitted is True
    assert message != "Deferred"
    kwargs = pr.create_review.call_args.kwargs
    assert kwargs["commit"] == ANALYSED
    assert extract_review_metadata(kwargs["body"])["sha"] == ANALYSED


def test_submits_pinned_with_explicit_model_name() -> None:
    """When model_name is provided, it is stamped into the review metadata."""
    pr = _pr(ANALYSED)

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        _message, submitted = _submit_formal_review(
            pr,
            "{}",
            "COMMENT",
            "owner/repo#415",
            analyzed_sha=ANALYSED,
            model_name="gemini-2.5-flash",
        )

    assert submitted is True
    kwargs = pr.create_review.call_args.kwargs
    meta = extract_review_metadata(kwargs["body"])
    assert meta["sha"] == ANALYSED
    assert meta["model"] == "gemini-2.5-flash"


def test_submits_pinned_with_state_model_name() -> None:
    """When model_name is carried in state dict, it is stamped into review metadata."""
    pr = _pr(ANALYSED)

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        _message, submitted = _submit_formal_review(
            pr,
            "{}",
            "COMMENT",
            "owner/repo#415",
            analyzed_sha=ANALYSED,
            state={"model_name": "gemini-2.5-pro"},
        )

    assert submitted is True
    kwargs = pr.create_review.call_args.kwargs
    meta = extract_review_metadata(kwargs["body"])
    assert meta["sha"] == ANALYSED
    assert meta["model"] == "gemini-2.5-pro"


def test_duplicate_check_is_keyed_to_analysed_commit() -> None:
    """An existing bot review at another commit must not block this commit's review."""
    pr = _pr(ANALYSED, reviews=[_bot_review(MOVED_TO)])

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        _, submitted = _submit_formal_review(
            pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED
        )

    assert submitted is True
    pr.create_review.assert_called_once()


def test_duplicate_at_same_commit_is_skipped() -> None:
    """Re-reviewing the identical commit stays suppressed."""
    pr = _pr(ANALYSED, reviews=[_bot_review(ANALYSED)])

    message, submitted = _submit_formal_review(
        pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED
    )

    assert submitted is False
    assert message.startswith("Skipped:")
    pr.create_review.assert_not_called()


def test_duplicate_at_same_commit_submits_when_force() -> None:
    """When force=True is passed, duplicate check is bypassed and review submits."""
    pr = _pr(ANALYSED, reviews=[_bot_review(ANALYSED)])

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        message, submitted = _submit_formal_review(
            pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED, force=True
        )

    assert submitted is True
    assert message.startswith("Submitted review")
    registry.claim.assert_called_once_with(
        "owner/repo", 415, ANALYSED, github_review_exists=False, force=True
    )
    pr.create_review.assert_called_once()


@pytest.mark.parametrize("comment", ["/review", "/re-review", "/force-review"])
def test_duplicate_at_same_commit_submits_when_explicit_comment_in_state(comment: str) -> None:
    """When user requests review via comment in state, duplicate check is bypassed."""
    pr = _pr(ANALYSED, reviews=[_bot_review(ANALYSED)])

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        message, submitted = _submit_formal_review(
            pr,
            "{}",
            "COMMENT",
            "owner/repo#415",
            state={"comment_body": comment},
            analyzed_sha=ANALYSED,
        )

    assert submitted is True
    assert message.startswith("Submitted review")
    registry.claim.assert_called_once_with(
        "owner/repo", 415, ANALYSED, github_review_exists=False, force=True
    )
    pr.create_review.assert_called_once()


def test_stamp_review_metadata_merges_existing_fields() -> None:
    """Stamping preserves the schema's metadata and replaces the comment in place."""
    body = '## Review\n\n<!-- hannibal-review-metadata: {"version":1,"verdict":"APPROVE"} -->\n'

    stamped = stamp_review_metadata(body, sha=ANALYSED)

    assert stamped.startswith("## Review")
    assert extract_review_metadata(stamped) == {
        "version": 1,
        "verdict": "APPROVE",
        "sha": ANALYSED,
    }
    assert stamped.count("hannibal-review-metadata") == 1


def test_stamp_review_metadata_appends_when_absent() -> None:
    """Bodies without metadata still get a self-describing comment."""
    stamped = stamp_review_metadata("Plain review body", sha=ANALYSED)

    assert stamped.startswith("Plain review body")
    assert extract_review_metadata(stamped) == {"sha": ANALYSED}


def test_submits_pinned_to_analysed_commit_pygithub_pull_request_instance() -> None:
    """On a real PyGithub PullRequest instance, a Commit object wrapping the SHA is passed."""
    from github.Commit import Commit
    from github.PullRequest import PullRequest

    mock_requester = MagicMock()
    mock_requester.requestJsonAndCheck.return_value = ({}, {})
    pr = PullRequest(
        mock_requester,
        headers={},
        attributes={
            "url": "https://api.github.com/repos/owner/repo/pulls/415",
            "head": {"sha": ANALYSED},
        },
        completed=True,
    )

    with (
        patch(
            "webhook_agent.review.review_enforcer._enforce_verdict",
            return_value=("Rendered body", "COMMENT", []),
        ),
        patch("webhook_agent.review.review_enforcer.review_claim_registry") as registry,
    ):
        registry.claim.return_value = object()
        message, submitted = _submit_formal_review(
            pr, "{}", "COMMENT", "owner/repo#415", analyzed_sha=ANALYSED
        )

    assert submitted is True
    assert message != "Deferred"
    post_calls = [c for c in mock_requester.requestJsonAndCheck.call_args_list if c[0][0] == "POST"]
    assert len(post_calls) == 1
    method, url = post_calls[0][0][:2]
    assert method == "POST"
    assert url == "https://api.github.com/repos/owner/repo/pulls/415/reviews"
    call_input = post_calls[0].kwargs["input"]
    assert call_input["commit_id"] == ANALYSED
