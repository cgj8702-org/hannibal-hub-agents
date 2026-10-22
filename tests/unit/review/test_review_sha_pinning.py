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
