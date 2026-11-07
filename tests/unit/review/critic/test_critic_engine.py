"""Unit tests for CriticEngine candidate evaluation, filtering, and demotion."""

from __future__ import annotations

from webhook_agent.review.critic import CriticEngine
from webhook_agent.review.schemas import IssueItem


def test_critic_engine_partition_and_filter_issues() -> None:
    """Verify that CriticEngine demotes moderate confidence critical issues to minor suggestions instead of vanishing them."""
    critic = CriticEngine(min_confidence=8)

    critical_issues = [
        IssueItem(
            path="src/service.py",
            line=40,
            description="High confidence blocking null dereference",
            suggested_fix="if x is None: return",
            category="LOGIC",
            confidence_score=9,
        ),
        IssueItem(
            path="src/service.py",
            line=55,
            description="Moderate confidence race condition on shared cache",
            suggested_fix="async with lock: cache[k] = v",
            category="CONCURRENCY",
            confidence_score=6,
        ),
        IssueItem(
            path="src/service.py",
            line=60,
            description="[NIT] Add type annotation to helper",
            suggested_fix="def helper(x: int) -> int:",
            category="NIT",
            confidence_score=8,
        ),
        IssueItem(
            path="src/service.py",
            line=70,
            description="Very speculative issue with no evidence",
            suggested_fix="",
            category="LOGIC",
            confidence_score=3,
        ),
    ]

    minor_suggestions = [
        IssueItem(
            path="src/service.py",
            line=80,
            description="Consider adding docstring example",
            suggested_fix="",
            category="MAINTAINABILITY",
            confidence_score=6,
        ),
        IssueItem(
            path="src/service.py",
            line=90,
            description="Pure noise suggestion",
            suggested_fix="",
            category="MAINTAINABILITY",
            confidence_score=2,
        ),
    ]

    surviving_crit, surviving_minor = critic.partition_and_filter_issues(
        critical_issues, minor_suggestions
    )

    # High confidence (9) stays in critical
    assert len(surviving_crit) == 1
    assert surviving_crit[0].description == "High confidence blocking null dereference"

    # Moderate confidence (6) demoted from critical to minor, plus original minor (6) survived
    # NIT dropped, low confidence (3, 2) dropped
    assert len(surviving_minor) == 2
    minor_descs = {m.description for m in surviving_minor}
    assert "Moderate confidence race condition on shared cache" in minor_descs
    assert "Consider adding docstring example" in minor_descs


def test_critic_engine_filter_issues_custom_min_confidence() -> None:
    """Verify filter_issues respects explicit min_confidence threshold."""
    critic = CriticEngine(min_confidence=8)

    issues = [
        IssueItem(
            path="src/app.py",
            line=10,
            description="Issue A",
            confidence_score=6,
        ),
        IssueItem(
            path="src/app.py",
            line=20,
            description="Issue B",
            confidence_score=4,
        ),
    ]

    # Default threshold (8) filters both
    assert len(critic.filter_issues(issues)) == 0

    # Custom threshold (5) keeps Issue A (confidence 6)
    survivors = critic.filter_issues(issues, min_confidence=5)
    assert len(survivors) == 1
    assert survivors[0].description == "Issue A"
