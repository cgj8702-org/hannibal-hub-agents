"""Unit tests for Smart Webhook Auditor tools (sequential thinking, codebase search) and code block formatting."""

from __future__ import annotations

from unittest.mock import MagicMock

from webhook_agent.formatter import (
    CodeReviewResponse,
    _format_suggested_fix_markdown,
    render_code_review_markdown,
)
from webhook_agent.schemas import IssueItem
from webhook_agent.tools.codebase_search import search_codebase
from webhook_agent.tools.sequential_thinking import sequential_thinking


def test_sequential_thinking_tool():
    """Verify sequential_thinking records thought history into context state."""
    ctx = MagicMock()
    ctx.state = {}
    res1 = sequential_thinking(
        ctx,
        thought="Analyzing diff for potential null dereference",
        thought_number=1,
        total_thoughts=2,
        next_thought_needed=True,
    )
    assert "Recorded thought 1/2" in res1
    assert len(ctx.state["sequential_thoughts"]) == 1

    res2 = sequential_thinking(
        ctx,
        thought="Verified null check exists in upstream caller",
        thought_number=2,
        total_thoughts=2,
        next_thought_needed=False,
    )
    assert "Thinking complete" in res2
    assert len(ctx.state["sequential_thoughts"]) == 2


def test_codebase_search_tool():
    """Verify search_codebase finds matching code snippets in repo."""
    ctx = MagicMock()
    ctx.state = {}
    res = search_codebase(ctx, query="def render_code_review_markdown")
    assert "Codebase Search Results" in res
    assert "formatter.py" in res


def test_format_suggested_fix_markdown_single_line():
    """Verify single-line suggested fix formatting with backticks."""
    fix_str = "x = y + 1"
    formatted = _format_suggested_fix_markdown(fix_str)
    assert formatted == "\n  * *Suggested Fix*: `x = y + 1`"


def test_format_suggested_fix_markdown_multi_line():
    """Verify multi-line suggested fix formatting with markdown code fence."""
    fix_str = "if x:\n    return True\nreturn False"
    formatted = _format_suggested_fix_markdown(fix_str)
    assert "```" in formatted
    assert "  * *Suggested Fix*:" in formatted
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
