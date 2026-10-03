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


def test_review_rejects_python_pr_without_ast_verification(mock_ctx):
    mock_ctx.state["deterministic_changed_files"] = ["src/webhook_agent/core/agent_definition.py"]
    mock_ctx.state["tools_executed"] = ["search_codebase"]
    payload = {
        "executive_summary": "Test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "COMMENT")
    assert res.startswith("Error: Review submission rejected")
    assert (
        "mandatory AST integrity and defect verification tool ('verify_python_ast') was not executed"
        in res
    )


@patch("webhook_agent.core.github_tools._submit_formal_review")
def test_review_allows_python_pr_with_ast_verification(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
    mock_ctx.state["deterministic_changed_files"] = ["src/webhook_agent/core/agent_definition.py"]
    mock_ctx.state["tools_executed"] = ["verify_python_ast"]
    payload = {
        "executive_summary": "Valid review after ast check",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "COMMENT")
    assert res == "Review submitted"
    assert mock_submit.called


@patch("webhook_agent.core.github_tools._submit_formal_review")
def test_review_skips_investigation_gate_when_no_python_files(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
    mock_ctx.state["deterministic_changed_files"] = ["README.md", "docs/architecture.md"]
    mock_ctx.state["tools_executed"] = []
    payload = {
        "executive_summary": "Docs review",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "COMMENT")
    assert res == "Review submitted"
    assert mock_submit.called


def test_review_rejects_approve_without_verified_invariants(mock_ctx):
    mock_ctx.state["deterministic_changed_files"] = ["README.md"]
    payload = {
        "executive_summary": "PR looks great, approving.",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "APPROVE")
    assert res.startswith("Error: Review submission rejected")
    assert "APPROVE event specified, but 'verified_invariants' is empty" in res


def test_review_rejects_approve_with_invalid_invariant(mock_ctx):
    mock_ctx.state["deterministic_changed_files"] = ["README.md"]
    payload = {
        "executive_summary": "PR looks great, approving.",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [
            {
                "invariant": "Preserves backwards compatibility",
                "path": "codebase",
                "line": 0,
                "evidence": "",
            }
        ],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "APPROVE")
    assert res.startswith("Error: Review submission rejected")
    assert "lacks exact file path" in res
    assert "lacks valid line number" in res
    assert "lacks concrete evidence" in res


@patch("webhook_agent.core.github_tools._submit_formal_review")
def test_review_accepts_approve_with_valid_verified_invariants(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
    mock_ctx.state["deterministic_changed_files"] = ["src/webhook_agent/core/agent_definition.py"]
    mock_ctx.state["tools_executed"] = ["verify_python_ast"]
    payload = {
        "executive_summary": "PR verified and approved.",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [
            {
                "invariant": "Rate limiter check remains non-blocking for paid tier",
                "path": "src/webhook_agent/core/agent_definition.py",
                "line": 105,
                "evidence": "Asserted in test_rate_limiter.py:test_paid_tier_waiter",
            }
        ],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "APPROVE")
    assert res == "Review submitted"
    assert mock_submit.called
