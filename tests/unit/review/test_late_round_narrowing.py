"""Unit tests for late-round blocker narrowing in review enforcement.

Validates that:
1. Rounds 1 and 2 preserve minor suggestions and normal verdict calculation.
2. Round 3+ suppresses minor suggestions to avoid review fatigue and moving goalposts.
3. Round 3+ upgrades COMMENT to APPROVE if all critical blockers and scorecard dimensions are clear.
4. Critical issues and unresolved prior blockers still enforce REQUEST_CHANGES on late rounds.
5. Scorecard warnings prevent upgrading COMMENT to APPROVE on late rounds.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.review.critic import CriticEngine
from webhook_agent.review.review_enforcer import _finalize_enforced_review
from webhook_agent.review.schemas import (
    IssueItem,
    ScorecardItem,
    SyncResolutionItem,
    SyncReviewResponse,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


def _build_sync_review(
    *,
    verdict: str = "COMMENT",
    critical_issues: list[IssueItem] | None = None,
    minor_suggestions: list[IssueItem] | None = None,
    scorecard: list[ScorecardItem] | None = None,
    resolutions: list[SyncResolutionItem] | None = None,
) -> SyncReviewResponse:
    return SyncReviewResponse(
        summary="PR synchronization audit.",
        verdict=verdict,
        scorecard=scorecard
        if scorecard is not None
        else [
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="All checks pass."),
            ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Thread safe."),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="No leaks."),
            ScorecardItem(
                dimension="Contract Integrity", status="PASS", notes="Signatures intact."
            ),
            ScorecardItem(
                dimension="Test Coverage & Regressions", status="PASS", notes="Tests pass."
            ),
        ],
        critical_issues=critical_issues or [],
        minor_suggestions=minor_suggestions or [],
        resolutions=resolutions
        if resolutions is not None
        else [
            SyncResolutionItem(
                item_description="Prior bug",
                status="RESOLVED",
                category="CRITICAL",
                evidence="Fixed in commit diff",
            )
        ],
        verified_invariants=[],
        semantic_invariants=[],
    )


def test_rounds_1_and_2_preserve_minor_suggestions() -> None:
    """Verify that initial round and round 2 keep minor suggestions without narrowing."""
    critic_engine = CriticEngine()
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 1\n"
    minor = [IssueItem(path="src/app.py", line=1, description="Consider renaming x to count")]

    for round_num in (1, 2):
        review = _build_sync_review(minor_suggestions=list(minor), verdict="COMMENT")
        _, verdict, _ = _finalize_enforced_review(
            review,
            critic_engine=critic_engine,
            det_contract_item=None,
            det_security_item=None,
            det_test_item=None,
            diff_for_grounding=diff_text,
            diff_text=diff_text,
            changed_paths=["src/app.py"],
            head_sha="head123",
            pr=MagicMock(),
            pr_label="test-pr",
            bot_reviews=[],
            existing_comments_data=[],
            review_budget=5,
            has_prior_reviews=True,
            is_intended_request_changes=False,
            is_body_comment=False,
            is_caller_comment=False,
            round_number=round_num,
        )
        assert len(review.minor_suggestions) == 1
        assert verdict == "COMMENT"


def test_round_3_plus_prunes_minor_suggestions_and_approves() -> None:
    """Verify round 3+ prunes minor suggestions and upgrades verdict to APPROVE when blockers clear."""
    critic_engine = CriticEngine()
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 1\n"
    minor = [IssueItem(path="src/app.py", line=1, description="Consider renaming x to count")]

    review = _build_sync_review(minor_suggestions=minor, verdict="COMMENT")
    _, verdict, _ = _finalize_enforced_review(
        review,
        critic_engine=critic_engine,
        det_contract_item=None,
        det_security_item=None,
        det_test_item=None,
        diff_for_grounding=diff_text,
        diff_text=diff_text,
        changed_paths=["src/app.py"],
        head_sha="head123",
        pr=MagicMock(),
        pr_label="test-pr",
        bot_reviews=[],
        existing_comments_data=[],
        review_budget=5,
        has_prior_reviews=True,
        is_intended_request_changes=False,
        is_body_comment=False,
        is_caller_comment=False,
        round_number=3,
    )
    # Minor suggestions are pruned to avoid moving goalposts
    assert review.minor_suggestions == []
    # With all blockers clear, verdict is upgraded to APPROVE
    assert verdict == "APPROVE"


def test_round_3_plus_preserves_critical_blockers() -> None:
    """Verify round 3+ retains critical issues and enforces REQUEST_CHANGES."""
    critic_engine = CriticEngine()
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 1\n"
    critical = [
        IssueItem(
            path="src/app.py",
            line=1,
            description="Null pointer dereference",
            suggested_fix="if x is not None: pass",
        )
    ]
    minor = [IssueItem(path="src/app.py", line=1, description="Consider renaming x to count")]

    review = _build_sync_review(
        critical_issues=critical, minor_suggestions=minor, verdict="COMMENT"
    )
    _, verdict, _ = _finalize_enforced_review(
        review,
        critic_engine=critic_engine,
        det_contract_item=None,
        det_security_item=None,
        det_test_item=None,
        diff_for_grounding=diff_text,
        diff_text=diff_text,
        changed_paths=["src/app.py"],
        head_sha="head123",
        pr=MagicMock(),
        pr_label="test-pr",
        bot_reviews=[],
        existing_comments_data=[],
        review_budget=5,
        has_prior_reviews=True,
        is_intended_request_changes=False,
        is_body_comment=False,
        is_caller_comment=False,
        round_number=4,
    )
    assert review.minor_suggestions == []
    assert len(review.critical_issues) == 1
    assert verdict == "REQUEST_CHANGES"


def test_round_3_plus_does_not_upgrade_on_scorecard_warning() -> None:
    """Verify round 3+ does not upgrade to APPROVE if a scorecard dimension has WARN."""
    critic_engine = CriticEngine()
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 1\n"
    minor = [IssueItem(path="src/app.py", line=1, description="Consider renaming x to count")]
    warn_scorecard = [
        ScorecardItem(
            dimension="Contract Integrity",
            status="WARN",
            notes="Suspected breaking signature change.",
        )
    ]

    review = _build_sync_review(
        minor_suggestions=minor, scorecard=warn_scorecard, verdict="COMMENT"
    )
    _, verdict, _ = _finalize_enforced_review(
        review,
        critic_engine=critic_engine,
        det_contract_item=None,
        det_security_item=None,
        det_test_item=None,
        diff_for_grounding=diff_text,
        diff_text=diff_text,
        changed_paths=["src/app.py"],
        head_sha="head123",
        pr=MagicMock(),
        pr_label="test-pr",
        bot_reviews=[],
        existing_comments_data=[],
        review_budget=5,
        has_prior_reviews=True,
        is_intended_request_changes=False,
        is_body_comment=False,
        is_caller_comment=False,
        round_number=3,
    )
    assert review.minor_suggestions == []
    # Scorecard warning prevents upgrade to APPROVE
    assert verdict == "COMMENT"


def test_round_3_plus_does_not_upgrade_on_unresolved_critical() -> None:
    """Verify round 3+ does not upgrade to APPROVE if prior critical issue is unresolved."""
    critic_engine = CriticEngine()
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 1\n"
    minor = [IssueItem(path="src/app.py", line=1, description="Consider renaming x to count")]
    unresolved = [
        SyncResolutionItem(
            item_description="Unfixed race condition",
            status="UNRESOLVED",
            category="CRITICAL",
            evidence="Mutex still omitted",
        )
    ]

    review = _build_sync_review(minor_suggestions=minor, resolutions=unresolved, verdict="COMMENT")
    prior_mock = MagicMock()
    prior_mock.body = "### Critical Issues\n- src/app.py:1: Unfixed race condition"
    prior_mock.state = "CHANGES_REQUESTED"

    _, verdict, _ = _finalize_enforced_review(
        review,
        critic_engine=critic_engine,
        det_contract_item=None,
        det_security_item=None,
        det_test_item=None,
        diff_for_grounding=diff_text,
        diff_text=diff_text,
        changed_paths=["src/app.py"],
        head_sha="head123",
        pr=MagicMock(),
        pr_label="test-pr",
        bot_reviews=[prior_mock],
        existing_comments_data=[],
        review_budget=5,
        has_prior_reviews=True,
        is_intended_request_changes=False,
        is_body_comment=False,
        is_caller_comment=False,
        round_number=3,
    )
    assert review.minor_suggestions == []
    assert verdict == "REQUEST_CHANGES"
