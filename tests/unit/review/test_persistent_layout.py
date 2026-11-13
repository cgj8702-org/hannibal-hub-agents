"""Unit tests for persistent 8-section Markdown layout.

Validates that:
1. All 8 sections are always rendered in fixed order 1..8 for CodeReviewResponse and SyncReviewResponse.
2. Empty sections render neutral '_None recorded._' or '_Initial review round; no prior resolutions._'.
3. Section numbering never shifts or vanishes between rounds.
4. Legacy markdown parsing can round-trip reviews rendered in the persistent layout.
"""

from __future__ import annotations

import pytest

from webhook_agent.review.formatter import (
    render_code_review_markdown,
    render_sync_review_markdown,
)
from webhook_agent.review.metadata import parse_legacy_review_findings
from webhook_agent.review.schemas import (
    CodeReviewResponse,
    IssueItem,
    ScorecardItem,
    SyncResolutionItem,
    SyncReviewResponse,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


def test_code_review_renders_all_8_sections_when_empty() -> None:
    """render_code_review_markdown renders all 8 sections in order even when empty."""
    cr = CodeReviewResponse(
        executive_summary="Empty review implementation.",
        scorecard=[],
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[],
        verified_invariants=[],
        semantic_invariants=[],
        unverified_claims=[],
    )
    md = render_code_review_markdown(cr, verdict="APPROVE")

    assert "### 1. Executive Summary" in md
    assert "### 2. Resolution Tracker" in md
    assert "_Initial review round; no prior resolutions._" in md
    assert "### 3. Audit Dimension Scorecard" in md
    assert "### 4. Action Items & Findings" in md
    assert "#### 🔴 Critical (Must Fix Before Merge)\n_None recorded._" in md
    assert "#### 🟡 Suggestions & Maintainability\n_None recorded._" in md
    assert "### 5. Potential Risks & Edge Cases\n\n_None recorded._" in md
    assert "### 6. Verified Invariants\n\n_None recorded._" in md
    assert "### 7. Semantic Invariants (Model-Asserted)\n\n_None recorded._" in md
    assert "### 8. Residual Uncertainty & Context Gaps\n\n_None recorded._" in md


def test_sync_review_renders_all_8_sections_when_empty() -> None:
    """render_sync_review_markdown renders all 8 sections in order even when empty."""
    sr = SyncReviewResponse(
        summary="Empty sync update.",
        resolutions=[],
        scorecard=[],
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[],
        verified_invariants=[],
        semantic_invariants=[],
        unverified_claims=[],
    )
    md = render_sync_review_markdown(sr, verdict="APPROVE")

    assert "### 1. Synchronization Summary" in md
    assert "### 2. Resolution Tracker\n\n_None recorded._" in md
    assert "### 3. Audit Dimension Scorecard" in md
    assert "### 4. New Findings (Introduced in Update)" in md
    assert "#### 🔴 Critical (Must Fix Before Merge)\n_None recorded._" in md
    assert "#### 🟡 Suggestions & Maintainability\n_None recorded._" in md
    assert "### 5. Potential Risks & Edge Cases\n\n_None recorded._" in md
    assert "### 6. Verified Invariants\n\n_None recorded._" in md
    assert "### 7. Semantic Invariants (Model-Asserted)\n\n_None recorded._" in md
    assert "### 8. Residual Uncertainty & Context Gaps\n\n_None recorded._" in md


def test_section_numbering_invariant_between_full_and_sparse_reviews() -> None:
    """Section numbers 1..8 stay strictly invariant regardless of section content."""
    sr_sparse = SyncReviewResponse(
        summary="Sparse sync review.",
        resolutions=[
            SyncResolutionItem(
                item_description="Bug fix verified",
                status="RESOLVED",
                evidence="diff checked",
            )
        ],
    )
    md = render_sync_review_markdown(sr_sparse, verdict="APPROVE")

    # Check order of section headers in the markdown text
    pos1 = md.index("### 1. Synchronization Summary")
    pos2 = md.index("### 2. Resolution Tracker")
    pos3 = md.index("### 3. Audit Dimension Scorecard")
    pos4 = md.index("### 4. New Findings (Introduced in Update)")
    pos5 = md.index("### 5. Potential Risks & Edge Cases")
    pos6 = md.index("### 6. Verified Invariants")
    pos7 = md.index("### 7. Semantic Invariants (Model-Asserted)")
    pos8 = md.index("### 8. Residual Uncertainty & Context Gaps")

    assert pos1 < pos2 < pos3 < pos4 < pos5 < pos6 < pos7 < pos8


def test_legacy_parser_round_trips_persistent_layout() -> None:
    """parse_legacy_review_findings accurately extracts findings rendered under persistent layout."""
    cr = CodeReviewResponse(
        executive_summary="Review with actionable issues.",
        critical_issues=[
            IssueItem(path="src/bug.py", line=42, description="Critical memory leak detected")
        ],
        minor_suggestions=[
            IssueItem(path="src/style.py", line=10, description="Improve variable name")
        ],
    )
    md = render_code_review_markdown(cr, verdict="REQUEST_CHANGES")

    legacy = parse_legacy_review_findings(md)
    assert len(legacy["critical_issues"]) == 1
    assert "Critical memory leak detected" in legacy["critical_issues"][0]["description"]
    assert "src/bug.py:42" in legacy["critical_issues"][0]["description"]

    assert len(legacy["minor_suggestions"]) == 1
    assert "Improve variable name" in legacy["minor_suggestions"][0]["description"]
    assert "src/style.py:10" in legacy["minor_suggestions"][0]["description"]
