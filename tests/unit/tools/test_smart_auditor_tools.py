"""Unit tests for Smart Webhook Auditor tools and code block formatting."""

from __future__ import annotations

from webhook_agent.review.formatter import (
    render_code_review_markdown,
)
from webhook_agent.review.schemas import CodeReviewResponse, IssueItem


def test_format_suggested_fix_markdown_single_line():
    """Verify single-line suggested fix formatting with backticks on IssueItem."""
    item = IssueItem(
        path="src/main.py", line=10, description="Test issue", suggested_fix="x = y + 1"
    )
    formatted = item.to_markdown()
    assert "* *Suggested Fix*: `x = y + 1`" in formatted


def test_format_suggested_fix_markdown_multi_line():
    """Verify multi-line suggested fix formatting with markdown code fence on IssueItem."""
    item = IssueItem(
        path="src/main.py",
        line=10,
        description="Test issue",
        suggested_fix="if x:\n    return True\nreturn False",
    )
    formatted = item.to_markdown()
    assert "```" in formatted
    assert "* *Suggested Fix*:" in formatted
    assert "    if x:" in formatted


def test_render_code_review_markdown_includes_formatted_fix():
    """Verify top-level PR review body renders suggested fixes nicely formatted."""
    review = CodeReviewResponse(
        executive_summary="Code review findings",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[
            IssueItem(
                path="src/webhook_agent/formatter.py",
                line=100,
                description="Refactor loop to single pass",
                suggested_fix="crit, sugg = [], []\nfor item in items:\n    pass",
            )
        ],
        risks_and_edge_cases=[],
    )
    rendered = render_code_review_markdown(review)
    assert "```" in rendered
    assert "crit, sugg = [], []" in rendered
