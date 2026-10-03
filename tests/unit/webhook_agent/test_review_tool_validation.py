"""Unit tests for strict finding validation in the review() tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents.context import Context

from webhook_agent.core.github_tools import review

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=Context)
    ctx.state = {
        "gh_client": MagicMock(),
        "repo_full_name": "owner/repo",
        "formal_review_eligible": True,
    }
    return ctx


def test_review_rejects_malformed_json(mock_ctx):
    res = review(mock_ctx, 123, "not a json string", "APPROVE")
    assert res.startswith("Error: Review submission rejected")
    assert "must be a valid JSON string" in res


def test_review_rejects_generic_codebase_path(mock_ctx):
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": "codebase",
                "line": 42,
                "description": "Critical flaw detected",
                "suggested_fix": "return True",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "lacks exact file path" in res


def test_review_rejects_missing_line_number(mock_ctx):
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": "src/main.py",
                "line": None,
                "description": "Critical flaw detected",
                "suggested_fix": "return True",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "lacks valid line number" in res


def test_review_rejects_missing_suggested_fix(mock_ctx):
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": "src/main.py",
                "line": 15,
                "description": "Critical flaw detected",
                "suggested_fix": "",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "lacks concrete suggested_fix replacement code" in res


def test_review_rejects_boilerplate_suggested_fix(mock_ctx):
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": "src/main.py",
                "line": 15,
                "description": "Critical flaw detected",
                "suggested_fix": "Address requested changes before merge.",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "generic boilerplate suggested_fix" in res


def test_review_rejects_request_changes_without_critical_issues(mock_ctx):
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "REQUEST_CHANGES event specified, but critical_issues is empty" in res


def test_review_rejects_sync_request_changes_without_actionable_items(mock_ctx):
    payload = {
        "summary": "Sync update",
        "confidence": 5,
        "resolutions": [
            {
                "item_description": "Prior item",
                "status": "RESOLVED",
                "evidence": "fixed",
                "category": "CRITICAL",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "critical_issues is empty and no prior items are UNRESOLVED" in res


@patch("webhook_agent.core.github_tools._submit_formal_review")
def test_review_accepts_valid_payload(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
    payload = {
        "executive_summary": "Valid initial review",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": "src/model_chain.py",
                "line": 42,
                "description": "IndexError on model name split",
                "suggested_fix": "model.removeprefix('models/')",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert mock_submit.called
