"""Unit tests for schemas, formatter, metadata, and grounding hardening (Track A)."""

from __future__ import annotations

import pytest

from webhook_agent.review.formatter import render_sync_review_markdown
from webhook_agent.review.grounding.heuristics import extract_bare_symbols
from webhook_agent.review.grounding.resolutions import verify_resolutions
from webhook_agent.review.metadata import (
    get_actionable_findings,
    parse_legacy_review_findings,
    serialize_review_metadata,
)
from webhook_agent.review.schemas import (
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    ScorecardItem,
    SyncResolutionItem,
    SyncReviewResponse,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_sync_resolution_item_preserves_path_and_line() -> None:
    """SyncResolutionItem preserves and coerces path and line aliases."""
    item = SyncResolutionItem.model_validate(
        {
            "item_description": "Fix bug",
            "status": "RESOLVED",
            "evidence": "Fixed in code",
            "category": "SUGGESTION",
            "file_path": "tests/unit/dev/test_foo.py",
            "line_number": 42,
        }
    )
    assert item.path == "tests/unit/dev/test_foo.py"
    assert item.line == 42
    assert item.category == "SUGGESTION"


def test_scorecard_item_to_table_row_escapes_newlines() -> None:
    """ScorecardItem.to_table_row replaces newlines in notes with spaces to preserve table rows."""
    item = ScorecardItem(
        dimension="Logic & Boundaries",
        status="PASS",
        notes="First line of rationale.\nSecond line with details.\nThird line.",
    )
    row = item.to_table_row()
    assert "\n" not in row
    assert "First line of rationale. Second line with details. Third line." in row
    assert row.startswith("| **Logic & Boundaries** | `✅ PASS` |")


def test_issue_item_to_markdown_syntax_highlighting() -> None:
    """IssueItem.to_markdown includes language tag for multi-line suggested fix."""
    item = IssueItem(
        path="src/hannibal/worker.py",
        line=10,
        description="Missing defensive check",
        suggested_fix="def handle():\n    return 42",
    )
    md = item.to_markdown("🔴")
    assert "```python" in md
    assert "def handle():" in md


def test_summary_aliases_and_sync_risks() -> None:
    """CodeReviewResponse and SyncReviewResponse support cross-compatible summary aliases and risks."""
    cr = CodeReviewResponse(
        executive_summary="Initial audit summary.",
    )
    assert cr.summary == "Initial audit summary."

    sr = SyncReviewResponse(
        summary="Sync audit update.",
        risks_and_edge_cases=[
            RiskItem(risk="Traffic burst spike", recommendation="Enable rate limiting")
        ],
    )
    assert sr.executive_summary == "Sync audit update."
    assert len(sr.risks_and_edge_cases) == 1

    md = render_sync_review_markdown(sr)
    assert "Potential Risks & Edge Cases" in md
    assert "Traffic burst spike" in md
    assert "Enable rate limiting" in md


def test_metadata_serialization_escapes_comment_close() -> None:
    """serialize_review_metadata escapes '-->' to prevent comment truncation."""
    data = {"notes": "Check lambda: x --> y boundary"}
    comment = serialize_review_metadata(data)
    assert comment.startswith("<!-- hannibal-review-metadata:")
    assert comment.endswith("-->")
    # Inside the JSON, '-->' is escaped
    assert "--\\u003e" in comment


def test_parse_legacy_review_findings_supports_h3_and_h4() -> None:
    """parse_legacy_review_findings matches both H3 and H4 section headers."""
    initial_body = """
## 🛡️ Code Review: `COMMENT`

### 1. Executive Summary
Overview.

### 2. Audit Dimension Scorecard
| Dimension | Status | Notes |
| :--- | :---: | :--- |

### 3. Critical Issues (Must Fix Before Merge)
* 🔴 `src/bad.py:10`: Critical syntax error

### 4. Minor Suggestions & Maintainability
* 🟡 `src/style.py:20`: Consider renaming variable
"""
    legacy = parse_legacy_review_findings(initial_body)
    assert len(legacy["critical_issues"]) == 1
    assert "Critical syntax error" in legacy["critical_issues"][0]["description"]
    assert len(legacy["minor_suggestions"]) == 1
    assert "Consider renaming variable" in legacy["minor_suggestions"][0]["description"]


def test_get_actionable_findings_ignores_scorecard_rows() -> None:
    """get_actionable_findings does not convert scorecard rows into actionable findings."""
    body_with_scorecard_warn = """
## 🛡️ Code Review: `COMMENT`

### 1. Executive Summary
Audit.

<!-- hannibal-review-metadata: {"scorecard":[{"dimension":"Contract Integrity","status":"WARN","notes":"Warning identified by symbol impact analysis."},{"dimension":"Test Coverage & Regressions","status":"WARN","notes":"Modified code lacks direct unit test coverage."}],"minor_suggestions":[{"path":"src/app.py","line":10,"description":"Consider refactoring helper"}]} -->
"""
    findings = get_actionable_findings(body_with_scorecard_warn)
    # Scorecard rows are ignored; only real findings (minor_suggestions, critical_issues, risks) are returned
    assert len(findings) == 1
    assert findings[0]["category"] == "SUGGESTION"
    assert findings[0]["description"] == "Consider refactoring helper"


def test_grounding_bare_symbols_masks_paths_and_detects_pascal_case() -> None:
    """extract_bare_symbols masks filepaths and detects PascalCase class names."""
    text = "Added tests/unit/dev/test_adk_doc_loop.py with coverage for DocLoopOrchestrator and EscalationChecker."
    symbols = extract_bare_symbols(text)
    # Path stem test_adk_doc_loop must NOT be extracted as a code symbol
    assert "test_adk_doc_loop" not in symbols
    # PascalCase classes must be extracted
    assert "doclooporchestrator" in symbols
    assert "escalationchecker" in symbols


def test_verify_resolutions_grounds_test_coverage_file_additions() -> None:
    """verify_resolutions accepts verified test file additions for test-coverage findings without code symbols."""
    diff = """--- a/tests/unit/dev/test_adk_doc_loop.py
+++ b/tests/unit/dev/test_adk_doc_loop.py
@@ -0,0 +1,15 @@
+def test_new_feature():
+    assert True
+"""
    res = [
        {
            "category": "SUGGESTION",
            "item_description": "Lack of test coverage for the doc processing logic.",
            "evidence": "Added tests/unit/dev/test_adk_doc_loop.py to cover doc processing logic.",
            "status": "RESOLVED",
        }
    ]
    verified = verify_resolutions(res, diff, ["tests/unit/dev/test_adk_doc_loop.py"])
    assert len(verified) == 1
    assert verified[0]["status"] == "RESOLVED"
