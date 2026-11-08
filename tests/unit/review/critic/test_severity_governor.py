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


def test_strip_self_assigned_critical_tag_from_comment_body() -> None:
    """Verify LLM self-assigned [CRITICAL] tags are stripped and governed to [MAJOR]."""
    gov = SeverityGovernor(deterministic_failing_locations={("src/proven.py", 12)})

    comments = [
        {
            "path": "src/unproven.py",
            "line": 42,
            "body": "[CRITICAL] LLM hallucinated critical failure",
        },
        {
            "path": "src/proven.py",
            "line": 12,
            "body": "[CRITICAL] Hard compiler error",
        },
    ]

    tagged = gov.tag_and_sort_comments(comments)

    assert len(tagged) == 2
    # Proven compiler error is kept as [CRITICAL]
    assert tagged[0]["path"] == "src/proven.py"
    assert tagged[0]["body"] == "[CRITICAL] Hard compiler error"

    # Unproven LLM finding is stripped and governed to [MAJOR]
    assert tagged[1]["path"] == "src/unproven.py"
    assert tagged[1]["body"] == "[MAJOR] LLM hallucinated critical failure"

    # Temporary sort/internal keys are popped
    assert "_sort_rank" not in tagged[0]
    assert "_sort_rank" not in tagged[1]
    assert "severity" not in tagged[0]
    assert "category" not in tagged[0]


def test_deterministic_categories_require_compiler_match() -> None:
    """Verify deterministic categories require verified compiler match to yield [CRITICAL]."""
    gov = SeverityGovernor(deterministic_failing_locations={("src/loop.py", 55)})

    # Proven loop with deterministic match
    cand_proven = CandidateFinding(
        file_path="src/loop.py",
        line_number=55,
        title="Infinite loop",
        description="Loop counter not incremented",
        category="PROVEN_LOOP",
    )
    assert gov.govern_severity(cand_proven) == TAG_CRITICAL

    # Unproven loop without deterministic match: governed to MAJOR
    cand_unproven = CandidateFinding(
        file_path="src/other.py",
        line_number=10,
        title="Potential loop",
        description="Model thinks this might loop",
        category="PROVEN_LOOP",
    )
    assert gov.govern_severity(cand_unproven) == TAG_MAJOR

    # Lockfile category without deterministic match: governed to MAJOR
    cand_lockfile_unproven = CandidateFinding(
        file_path="pyproject.toml",
        line_number=1,
        title="Lockfile issue",
        description="Unverified lockfile drift",
        category="LOCKFILE",
    )
    assert gov.govern_severity(cand_lockfile_unproven) == TAG_MAJOR


def test_govern_severity_critical_container_baseline() -> None:
    """Verify findings in critical_issues container inherit CRITICAL and never demote to MINOR."""
    gov = SeverityGovernor()
    # Unset severity inside critical_issues
    item = IssueItem(
        path="src/service.py",
        line=42,
        description="Severe resource leak",
    )
    # With default_severity="CRITICAL", inherits CRITICAL, governed to MAJOR (unproven), never MINOR
    assert gov.govern_severity(item, default_severity="CRITICAL") == TAG_MAJOR

    # With proven deterministic match, allowed to be CRITICAL
    gov_proven = SeverityGovernor(deterministic_failing_locations={("src/service.py", 42)})
    assert gov_proven.govern_severity(item, default_severity="CRITICAL") == TAG_CRITICAL


def test_govern_severity_boundary_and_architecture_categories() -> None:
    """Verify ARCHITECTURE, BOUNDARY, and CONTRACT categories map to TAG_MAJOR."""
    gov = SeverityGovernor()
    assert gov.govern_severity({"category": "ARCHITECTURE"}) == TAG_MAJOR
    assert gov.govern_severity({"category": "BOUNDARY"}) == TAG_MAJOR
    assert gov.govern_severity({"category": "CONTRACT"}) == TAG_MAJOR


def test_apply_governance_to_issues_container_baseline() -> None:
    """Verify apply_governance_to_issues updates issue.severity honoring container defaults."""
    gov = SeverityGovernor(deterministic_failing_locations={("src/proven.py", 12)})
    issues = [
        IssueItem(path="src/proven.py", line=12, description="Proven boundary issue"),
        IssueItem(path="src/unproven.py", line=34, description="Heuristic flaw"),
    ]
    governed = gov.apply_governance_to_issues(issues, default_severity="CRITICAL")
    assert governed[0].severity == "CRITICAL"
    assert governed[1].severity == "MAJOR"  # Floored at MAJOR, not demoted to MINOR!
