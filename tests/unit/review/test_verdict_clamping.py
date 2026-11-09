"""Unit tests for deterministic verdict clamping and monotonic strictness.

Validates that:
1. LLM cannot loosen mechanical verdicts: APPROVE cannot override unresolved items or failing dimensions.
2. Unresolved CRITICAL resolution forces REQUEST_CHANGES.
3. Unresolved non-critical resolution (SUGGESTION, RISK) forces COMMENT.
4. Scorecard FAIL forces REQUEST_CHANGES.
5. Scorecard WARN or UNKNOWN forces COMMENT.
6. Monotonic strictness is preserved: LLM can increase strictness, never loosen it.
7. Clean review with all items resolved approves.
"""

from __future__ import annotations

import pytest

from webhook_agent.review.formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    clamp_verdict,
)
from webhook_agent.review.schemas import (
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    ScorecardItem,
    SyncResolutionItem,
    SyncReviewResponse,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


def test_clamp_verdict_monotonic_strictness() -> None:
    """clamp_verdict allows LLM to increase strictness, never loosen it."""
    # LLM cannot loosen REQUEST_CHANGES
    assert clamp_verdict("REQUEST_CHANGES", "APPROVE") == "REQUEST_CHANGES"
    assert clamp_verdict("REQUEST_CHANGES", "COMMENT") == "REQUEST_CHANGES"
    assert clamp_verdict("REQUEST_CHANGES", None) == "REQUEST_CHANGES"

    # LLM cannot loosen COMMENT to APPROVE, but can tighten to REQUEST_CHANGES
    assert clamp_verdict("COMMENT", "APPROVE") == "COMMENT"
    assert clamp_verdict("COMMENT", "REQUEST_CHANGES") == "REQUEST_CHANGES"
    assert clamp_verdict("COMMENT", None) == "COMMENT"

    # LLM can tighten APPROVE to COMMENT or REQUEST_CHANGES
    assert clamp_verdict("APPROVE", "COMMENT") == "COMMENT"
    assert clamp_verdict("APPROVE", "REQUEST_CHANGES") == "REQUEST_CHANGES"
    assert clamp_verdict("APPROVE", "APPROVE") == "APPROVE"
    assert clamp_verdict("APPROVE", None) == "APPROVE"


def test_sync_verdict_unresolved_critical_forces_request_changes() -> None:
    """An unresolved CRITICAL resolution strictly enforces REQUEST_CHANGES even if LLM said APPROVE."""
    sync_resp = SyncReviewResponse(
        summary="Incremental diff fixes.",
        verdict="APPROVE",
        resolutions=[
            SyncResolutionItem(
                item_description="SQL injection vulnerability in query builder",
                status="UNRESOLVED",
                category="CRITICAL",
                evidence="No parameterized query found in updated diff",
            )
        ],
        scorecard=[
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Verified clean"),
        ],
    )
    assert calculate_sync_verdict(sync_resp) == "REQUEST_CHANGES"


def test_sync_verdict_unresolved_suggestion_forces_comment() -> None:
    """An unresolved SUGGESTION resolution strictly enforces COMMENT (cannot APPROVE)."""
    sync_resp = SyncReviewResponse(
        summary="Incremental diff fixes.",
        verdict="APPROVE",
        resolutions=[
            SyncResolutionItem(
                item_description="Consider extracting helper function",
                status="UNRESOLVED",
                category="SUGGESTION",
                evidence="Deferred to future PR",
            )
        ],
        scorecard=[
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean"),
        ],
    )
    assert calculate_sync_verdict(sync_resp) == "COMMENT"


def test_sync_verdict_unresolved_risk_forces_comment() -> None:
    """An unresolved RISK resolution strictly enforces COMMENT (cannot APPROVE)."""
    sync_resp = SyncReviewResponse(
        summary="Incremental diff fixes.",
        verdict="APPROVE",
        resolutions=[
            SyncResolutionItem(
                item_description="Potential memory leak on repeated invocation",
                status="UNRESOLVED",
                category="RISK",
                evidence="Loop still caches unbounded results",
            )
        ],
        scorecard=[
            ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Clean"),
        ],
    )
    assert calculate_sync_verdict(sync_resp) == "COMMENT"


def test_sync_verdict_all_resolved_approves() -> None:
    """When all prior findings are resolved and dimensions are PASS, verdict is APPROVE."""
    sync_resp = SyncReviewResponse(
        summary="Incremental diff fixes.",
        verdict="APPROVE",
        resolutions=[
            SyncResolutionItem(
                item_description="Crash on empty input",
                status="RESOLVED",
                category="CRITICAL",
                evidence="Added guard statement in diff",
            ),
            SyncResolutionItem(
                item_description="Add missing unit tests",
                status="RESOLVED",
                category="SUGGESTION",
                evidence="Added tests/unit/test_feature.py",
            ),
        ],
        scorecard=[
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean"),
            ScorecardItem(dimension="Test Coverage & Regressions", status="PASS", notes="Clean"),
        ],
    )
    assert calculate_sync_verdict(sync_resp) == "APPROVE"


def test_strict_verdict_critical_issue_forces_request_changes() -> None:
    """Critical issues on CodeReviewResponse enforce REQUEST_CHANGES even if LLM said APPROVE."""
    cr = CodeReviewResponse(
        executive_summary="Initial code audit.",
        verdict="APPROVE",
        critical_issues=[
            IssueItem(
                path="src/main.py",
                line=15,
                description="Unhandled TypeError when config is None",
            )
        ],
    )
    assert calculate_strict_verdict(cr) == "REQUEST_CHANGES"


def test_strict_verdict_scorecard_fail_forces_request_changes() -> None:
    """A failing dimension on scorecard causes mechanical REQUEST_CHANGES."""
    cr = CodeReviewResponse(
        executive_summary="Initial code audit.",
        verdict="APPROVE",
        scorecard=[
            ScorecardItem(
                dimension="Contract Integrity",
                status="FAIL",
                notes="Breaking public API contract change detected",
            )
        ],
    )
    assert calculate_strict_verdict(cr) == "REQUEST_CHANGES"


def test_strict_verdict_clean_review_approves() -> None:
    """Clean CodeReviewResponse with PASS dimensions and no critical issues approves."""
    cr = CodeReviewResponse(
        executive_summary="Initial code audit clean.",
        verdict="APPROVE",
        scorecard=[
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean"),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Clean"),
        ],
        critical_issues=[],
        minor_suggestions=[],
    )
    assert calculate_strict_verdict(cr) == "APPROVE"
