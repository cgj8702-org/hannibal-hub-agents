"""Unit tests for SeverityGovernor in webhook_agent.review.critic."""

from __future__ import annotations

from webhook_agent.review.critic.severity_governor import (
    TAG_CRITICAL,
    TAG_MAJOR,
    TAG_MINOR,
    SeverityGovernor,
    apply_severity_tag,
    strip_severity_tag,
)
from webhook_agent.review.schemas import CandidateFinding, IssueItem


def test_strip_and_apply_severity_tag() -> None:
    assert strip_severity_tag("[CRITICAL] Syntax error") == "Syntax error"
    assert strip_severity_tag("[MAJOR]   Missing check") == "Missing check"
    assert strip_severity_tag("[MINOR] Docs update") == "Docs update"
    assert strip_severity_tag("Plain text") == "Plain text"

    assert apply_severity_tag("Syntax error", TAG_CRITICAL) == "[CRITICAL] Syntax error"
    assert apply_severity_tag("[MAJOR] Syntax error", TAG_CRITICAL) == "[CRITICAL] Syntax error"


def test_govern_severity_unproven_critical_downgraded_to_major() -> None:
    gov = SeverityGovernor()
    candidate = CandidateFinding(
        id="cand-1",
        file_path="src/main.py",
        line_number=10,
        severity="CRITICAL",
        confidence_score=9,
        title="Hallucinated critical bug",
        description="Model claims this is critical",
    )
    # Without matching deterministic location, governed to MAJOR
    assert gov.govern_severity(candidate) == TAG_MAJOR


def test_govern_severity_proven_critical_allowed() -> None:
    gov = SeverityGovernor(deterministic_failing_locations={("src/main.py", 10)})
    candidate = CandidateFinding(
        id="cand-1",
        file_path="src/main.py",
        line_number=10,
        severity="CRITICAL",
        confidence_score=10,
        title="Real compiler failure",
        description="Syntax error",
    )
    # With matching location, CRITICAL is preserved
    assert gov.govern_severity(candidate) == TAG_CRITICAL

    # Dict form with is_deterministic flag
    d_finding = {"severity": "CRITICAL", "file_path": "src/other.py", "line": 5}
    assert gov.govern_severity(d_finding, is_deterministic=True) == TAG_CRITICAL


def test_govern_severity_category_and_levels() -> None:
    gov = SeverityGovernor()

    item_high = IssueItem(
        path="src/app.py",
        line=15,
        severity="HIGH",
        description="High severity issue",
    )
    assert gov.govern_severity(item_high) == TAG_MAJOR

    item_med_logic = IssueItem(
        path="src/app.py",
        line=15,
        severity="MEDIUM",
        category="LOGIC",
        description="Logic flaw",
    )
    assert gov.govern_severity(item_med_logic) == TAG_MAJOR

    item_low_docs = IssueItem(
        path="src/app.py",
        line=15,
        severity="LOW",
        category="DOCS",
        description="Doc typo",
    )
    assert gov.govern_severity(item_low_docs) == TAG_MINOR


def test_tag_and_sort_comments() -> None:
    gov = SeverityGovernor(deterministic_failing_locations={("src/core.py", 5)})

    comments = [
        {
            "path": "src/b.py",
            "line": 20,
            "body": "Minor comment",
            "category": "MAINTAINABILITY",
        },
        {
            "path": "src/core.py",
            "line": 5,
            "body": "Syntax error",
            "severity": "CRITICAL",
        },
        {
            "path": "src/a.py",
            "line": 10,
            "body": "Major logic issue",
            "severity": "HIGH",
        },
    ]

    tagged_and_sorted = gov.tag_and_sort_comments(comments)

    # Order must be CRITICAL first, then MAJOR, then MINOR
    assert len(tagged_and_sorted) == 3
    assert tagged_and_sorted[0]["path"] == "src/core.py"
    assert tagged_and_sorted[0]["body"].startswith("[CRITICAL]")

    assert tagged_and_sorted[1]["path"] == "src/a.py"
    assert tagged_and_sorted[1]["body"].startswith("[MAJOR]")

    assert tagged_and_sorted[2]["path"] == "src/b.py"
    assert tagged_and_sorted[2]["body"].startswith("[MINOR]")
