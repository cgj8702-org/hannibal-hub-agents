"""Unit tests for the review replay engine and baseline report generator."""

from __future__ import annotations

from typing import Any

import pytest

from webhook_agent.review.grounding import Finding, GroundingIssue
from webhook_agent.review.replay import (
    BaselineReport,
    FixtureEvaluation,
    ReviewEvaluation,
    evaluate_fixture,
    evaluate_fixtures,
    evaluate_review,
    extract_findings_from_body,
    extract_findings_from_metadata,
    format_baseline_markdown,
    format_baseline_table,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

SAMPLE_DIFF = (
    "--- a/src/lib.py\n"
    "+++ b/src/lib.py\n"
    "@@ -10,3 +10,4 @@\n"
    " context_line_1\n"
    "+def process_event(event: dict) -> bool:\n"
    "+    return True\n"
    " context_line_2\n"
)


def test_extract_findings_from_metadata_dict_items() -> None:
    metadata: dict[str, Any] = {
        "critical_issues": [
            {
                "path": "src/lib.py",
                "line": 11,
                "description": "Missing return type",
                "suggested_fix": "def process_event(event: dict) -> bool:",
            }
        ],
        "minor_suggestions": [
            {
                "path": "src/lib.py",
                "line": 12,
                "description": "Consider logging",
                "suggested_fix": "logger.info('event')",
            }
        ],
    }
    findings = extract_findings_from_metadata(metadata)
    assert len(findings) == 2
    assert findings[0].kind == "critical"
    assert findings[0].path == "src/lib.py"
    assert findings[0].line == 11
    assert findings[1].kind == "suggestion"
    assert findings[1].path == "src/lib.py"
    assert findings[1].line == 12


def test_extract_findings_from_metadata_string_invariants_with_line_ranges() -> None:
    metadata: dict[str, Any] = {
        "verified_invariants": [
            "Dependencies are grouped (.github/dependabot.yml:11-19)",
            "Actions are grouped (`.github/dependabot.yml:21,27`)",
            "Deterministic contract evaluation floor holds without dynamic overrides.",
        ]
    }
    findings = extract_findings_from_metadata(metadata)
    # The two citations with paths should be extracted; the pure prose invariant is not a path citation
    assert len(findings) == 2
    assert findings[0].kind == "invariant"
    assert findings[0].path == ".github/dependabot.yml"
    assert findings[0].line == 11
    assert findings[1].kind == "invariant"
    assert findings[1].path == ".github/dependabot.yml"
    assert findings[1].line == 21


def test_evaluate_review_clean_grounding() -> None:
    review: dict[str, Any] = {
        "id": 101,
        "login": "hannibal-hub-agents[bot]",
        "commit_id": "abc1234",
        "body": (
            "## Review\n\n"
            "<!-- hannibal-review-metadata: "
            '{"version":1,"critical_issues":[],"minor_suggestions":[{"path":"src/lib.py","line":11,"evidence":"def process_event"}]} -->'
        ),
    }
    evaluation = evaluate_review(review, SAMPLE_DIFF, ["src/lib.py"])
    assert evaluation.review_id == 101
    assert evaluation.total_claims == 1
    assert evaluation.is_grounded is True
    assert evaluation.hard_issues == []


def test_evaluate_review_flags_hard_and_soft_issues() -> None:
    review: dict[str, Any] = {
        "id": 102,
        "login": "hannibal-hub-agents[bot]",
        "commit_id": "def5678",
        "body": (
            "## Review\n\n"
            "<!-- hannibal-review-metadata: "
            '{"version":1,"critical_issues":[{"path":"src/absent.py","line":5,"evidence":"fake"}],'
            '"minor_suggestions":[{"path":"src/lib.py","line":10,"evidence":"context"}]} -->'
        ),
    }
    evaluation = evaluate_review(review, SAMPLE_DIFF, ["src/lib.py"])
    assert evaluation.total_claims == 2
    assert evaluation.is_grounded is False
    assert len(evaluation.hard_issues) == 1
    assert evaluation.hard_issues[0].code == "path_not_changed"
    assert len(evaluation.soft_issues) == 1
    assert evaluation.soft_issues[0].code == "context_only_line"


def test_evaluate_fixture_and_report_aggregation() -> None:
    fixture: dict[str, Any] = {
        "number": 999,
        "repo": "cgj8702-org/test-repo",
        "title": "feat: test feature",
        "changed_files": ["src/lib.py"],
        "patches": {
            "src/lib.py": "@@ -10,3 +10,4 @@\n context_line_1\n+def process_event():\n+    pass\n context_line_2"
        },
        "reviews": [
            {
                "id": 201,
                "login": "hannibal-hub-agents[bot]",
                "commit_id": "c1",
                "body": (
                    "<!-- hannibal-review-metadata: "
                    '{"version":1,"critical_issues":[],"minor_suggestions":[{"path":"src/lib.py","line":11,"evidence":"def process_event"}]} -->'
                ),
            }
        ],
    }

    report: BaselineReport = evaluate_fixtures([fixture])
    assert report.total_prs == 1
    assert report.total_reviews == 1
    assert report.total_claims == 1
    assert report.total_hard_issues == 0
    assert report.overall_accuracy == 100.0
    assert report.hallucination_rate == 0.0

    # Format table and markdown
    table = format_baseline_table(report)
    assert "#999" in table
    assert "100.0%" in table
    assert "✅ PASS" in table

    md = format_baseline_markdown(report)
    assert "| #999 |" in md
    assert "`100.0%`" in md
