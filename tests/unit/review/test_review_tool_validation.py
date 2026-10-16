"""Unit tests for strict finding validation in the review() tool."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents.context import Context

from webhook_agent.tools.github_tools import review

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


_ALPHA_PATCH = "@@ -10,2 +10,3 @@\n existing_line\n+added_line\n"


def _grounding_ctx(patches, *, files_error=None):
    """Build a Context whose PR exposes the given {path: patch} mapping."""
    pr = MagicMock()
    if files_error is not None:
        pr.get_files.side_effect = files_error
    else:
        files = []
        for name, patch in patches.items():
            file_mock = MagicMock()
            file_mock.filename = name
            file_mock.patch = patch
            files.append(file_mock)
        pr.get_files.return_value = files
    repo = MagicMock()
    repo.get_pull.return_value = pr
    gh = MagicMock()
    gh.get_repo.return_value = repo
    ctx = MagicMock(spec=Context)
    ctx.state = {
        "gh_client": gh,
        "repo_full_name": "owner/repo",
        "formal_review_eligible": True,
        # Mirrors the prefetch path: without this the mandatory investigation gate
        # rejects any PR touching a .py file before the grounding check is reached.
        "deterministic_precompiled_ast": True,
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


def test_review_rejects_sync_request_changes_without_actionable_items():
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    payload = {
        "summary": "Sync update",
        "confidence": 5,
        "resolutions": [
            {
                "item_description": "Prior item",
                "status": "RESOLVED",
                "evidence": "added_line is added",
                "path": "src/alpha.py",
                "category": "CRITICAL",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = review(ctx, 123, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "critical_issues is empty and no prior items are UNRESOLVED" in res


@patch("webhook_agent.tools.github_tools._submit_formal_review")
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
    mock_ctx.state["tools_executed"] = ["read_file"]
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


@patch("webhook_agent.tools.github_tools._submit_formal_review")
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


def test_review_enforces_ast_gate_when_precompiled_ast_is_explicitly_false_with_stale_user_state(
    mock_ctx,
):
    """Verify that an explicit False in deterministic_precompiled_ast is preserved and doesn't leak True from stale user state."""
    mock_ctx.state["deterministic_changed_files"] = ["src/webhook_agent/core/agent_definition.py"]
    mock_ctx.state["deterministic_precompiled_ast"] = False
    mock_ctx.state["user:deterministic_precompiled_ast"] = True
    mock_ctx.state["tools_executed"] = ["read_file"]
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


@patch("webhook_agent.tools.github_tools._submit_formal_review")
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


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_review_allows_approve_without_verified_invariants(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
    mock_ctx.state["deterministic_changed_files"] = ["README.md"]
    payload = {
        "executive_summary": "PR looks great, approving.",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [],
        "unverified_claims": ["Could not verify performance under heavy load"],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "APPROVE")
    assert res == "Review submitted"
    assert mock_submit.called
    _pr, body_arg, event_arg, _key, _state = mock_submit.call_args[0]
    assert event_arg == "APPROVE"
    parsed = json.loads(body_arg)
    assert parsed.get("verdict") in ("APPROVE", None)


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_review_ignores_legacy_invariants_on_approve(mock_submit, mock_ctx):
    mock_submit.return_value = ("Review submitted", True)
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
                "line": 10,
                "evidence": "",
            }
        ],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "APPROVE")
    assert res == "Review submitted"
    assert mock_submit.called


@patch("webhook_agent.tools.github_tools._submit_formal_review")
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


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_review_handles_adk_state_object_properly(mock_submit, mock_ctx):
    """Verify review tool correctly reads non-dict ADK State objects with to_dict()."""
    mock_submit.return_value = ("Review submitted", True)

    class MockADKState:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return dict(self._data)

        def get(self, k, default=None):
            return self._data.get(k, default)

    mock_ctx.state = MockADKState(
        {
            "gh_client": mock_ctx.state["gh_client"],
            "repo_full_name": "owner/repo",
            "formal_review_eligible": True,
            "deterministic_changed_files": ["src/webhook_agent/core/agent_definition.py"],
            "deterministic_precompiled_ast": True,
            "tools_executed": ["verify_python_ast"],
        }
    )

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


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_review_accepts_advisory_minor_suggestion_with_empty_suggested_fix(mock_submit, mock_ctx):
    """Verify that review() tool accepts advisory minor suggestions with empty suggested_fix."""
    mock_submit.return_value = ("Review submitted", True)
    mock_ctx.state["deterministic_changed_files"] = ["README.md"]
    payload = {
        "executive_summary": "Docs review with advisory feedback.",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "README.md",
                "line": 10,
                "description": "Consider adding a quickstart section for new contributors.",
                "suggested_fix": "",
            }
        ],
        "context_gaps": [],
    }
    res = review(mock_ctx, 123, json.dumps(payload), "COMMENT")
    assert res == "Review submitted"
    assert mock_submit.called


# ---------------------------------------------------------------------------
# Phase 2.2: deterministic citation grounding (warn-only unless strict)
# ---------------------------------------------------------------------------


def _critical_payload(path, line):
    """A REQUEST_CHANGES payload that passes every pre-existing validation rule."""
    return {
        "executive_summary": "Grounding test summary",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [
            {
                "path": path,
                "line": line,
                "description": "Critical flaw detected",
                "suggested_fix": "return True",
            }
        ],
        "minor_suggestions": [],
        "context_gaps": [],
    }


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_does_not_reject_when_not_strict(mock_submit, monkeypatch):
    """A hard grounding failure is logged but not enforced by default."""
    monkeypatch.delenv("STRICT_REVIEW_GROUNDING", raising=False)
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    res = review(ctx, 8101, json.dumps(_critical_payload("src/beta.py", 12)), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert mock_submit.called


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_rejects_hard_failure_when_strict(mock_submit, monkeypatch):
    """With STRICT_REVIEW_GROUNDING set, an unciteable path rejects the review."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    res = review(ctx, 8102, json.dumps(_critical_payload("src/beta.py", 12)), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "src/beta.py" in res
    assert not mock_submit.called


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_soft_issues_never_reject_even_when_strict(mock_submit, monkeypatch):
    """A line the diff does not display is unverifiable, not wrong: do not reject."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    res = review(ctx, 8103, json.dumps(_critical_payload("src/alpha.py", 99)), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert mock_submit.called


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_skips_and_logs_when_patch_text_is_unavailable(
    mock_submit,
    monkeypatch,
    caplog,
):
    """'Cannot verify' must be reported and must not silently become a rejection."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({}, files_error=RuntimeError("no files"))
    with caplog.at_level(logging.WARNING):
        res = review(ctx, 8104, json.dumps(_critical_payload("src/beta.py", 12)), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert "Grounding check skipped" in caplog.text


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_partial_iteration_failure_skips_grounding(mock_submit, monkeypatch, caplog):
    """A partial get_files() iteration error marks retrieval_ok=False and skips grounding."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)

    def _partial_files():
        f1 = MagicMock()
        f1.filename = "src/alpha.py"
        f1.patch = _ALPHA_PATCH
        yield f1
        raise RuntimeError("Stream interrupted")

    pr = MagicMock()
    pr.get_files.side_effect = _partial_files
    repo = MagicMock()
    repo.get_pull.return_value = pr
    gh = MagicMock()
    gh.get_repo.return_value = repo
    ctx = MagicMock(spec=Context)
    ctx.state = {
        "gh_client": gh,
        "repo_full_name": "owner/repo",
        "formal_review_eligible": True,
        "deterministic_precompiled_ast": True,
    }

    with caplog.at_level(logging.WARNING):
        res = review(ctx, 8106, json.dumps(_critical_payload("src/beta.py", 12)), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert "Grounding check skipped" in caplog.text


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_all_none_patches_hits_skip(mock_submit, monkeypatch, caplog):
    """When all changed files have patch=None, grounding hits the skip warning."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"assets/logo.png": None, "data/bin.dat": None})
    with caplog.at_level(logging.WARNING):
        res = review(ctx, 8107, json.dumps(_critical_payload("src/beta.py", 12)), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert "Grounding check skipped" in caplog.text


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_strict_mode_skipped_grounding_downgrades_approve_to_comment(
    mock_submit, monkeypatch, caplog
):
    """Strict-mode review with skipped grounding never submits APPROVE, downgrading to COMMENT."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"assets/logo.png": None})
    payload = {
        "executive_summary": "Attempting approval on binary-only PR",
        "confidence": 5,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "context_gaps": [],
        "verified_invariants": [{"path": "src/alpha.py", "line": 10, "evidence": "check"}],
    }
    with caplog.at_level(logging.WARNING):
        res = review(ctx, 8108, json.dumps(payload), "APPROVE")
    assert res == "Review submitted"
    assert mock_submit.called
    _submitted_pr, _submitted_body, submitted_event, _target_key, _state = mock_submit.call_args[0]
    assert submitted_event == "COMMENT"
    assert "downgrading verdict from APPROVE to COMMENT" in caplog.text


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_grounding_rejects_unciteable_finding_when_strict(mock_submit, monkeypatch):
    """The APPROVE path is grounded too: a suggestion on an unchanged path is rejected."""
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    payload = {
        "executive_summary": "Grounding test summary",
        "confidence": 8,
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "src/gamma.py",
                "line": 5,
                "description": "Unchecked parameter",
                "suggested_fix": "check_param(x)",
            }
        ],
        "context_gaps": [],
    }
    res = review(ctx, 8105, json.dumps(payload), "APPROVE")
    assert res.startswith("Error: Review submission rejected")
    assert "src/gamma.py" in res


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_sync_review_request_changes_evaluates_after_grounding_allows_ungrounded_resolution(
    mock_submit,
):
    """Sync review requesting changes with empty critical_issues is allowed when grounding
    marks an ungrounded resolution as UNRESOLVED, and serialized body reflects grounded status.
    """
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    payload = {
        "summary": "Sync review",
        "resolutions": [
            {
                "item_description": "Fix bug in src/alpha.py",
                "status": "RESOLVED",
                "evidence": "unrelated_symbol fixed",
                "path": "src/alpha.py",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = review(ctx, 8109, json.dumps(payload), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert mock_submit.called
    _submitted_pr, submitted_body, submitted_event, _target_key, _state = mock_submit.call_args[0]
    assert submitted_event == "REQUEST_CHANGES"
    submitted_data = json.loads(submitted_body)
    assert submitted_data["resolutions"][0]["status"] == "UNRESOLVED"


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_sync_review_request_changes_rejected_when_resolutions_grounded_as_resolved_and_no_critical_issues(
    mock_submit,
):
    """Sync review requesting changes with empty critical_issues is rejected if all resolutions
    are grounded as RESOLVED.
    """
    mock_submit.return_value = ("Review submitted", True)
    ctx = _grounding_ctx({"src/alpha.py": _ALPHA_PATCH})
    payload = {
        "summary": "Sync review",
        "resolutions": [
            {
                "item_description": "Fix bug in src/alpha.py",
                "status": "RESOLVED",
                "evidence": "added_line is added",
                "path": "src/alpha.py",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = review(ctx, 8110, json.dumps(payload), "REQUEST_CHANGES")
    assert res.startswith("Error: Review submission rejected")
    assert "critical_issues is empty and no prior items are UNRESOLVED" in res
    assert not mock_submit.called


@patch("webhook_agent.tools.github_tools._submit_formal_review")
def test_sync_review_request_changes_grounds_resolutions_when_diff_text_empty(
    mock_submit,
):
    """When patch text is absent or empty, sync review still verifies resolutions,
    marking ungrounded RESOLVED claims as UNRESOLVED and updating serialized body.
    """
    mock_submit.return_value = ("Review submitted", True)
    # Context with empty patches
    ctx = _grounding_ctx({})
    payload = {
        "summary": "Sync review with empty patches",
        "resolutions": [
            {
                "item_description": "Fix bug in src/alpha.py",
                "status": "RESOLVED",
                "evidence": "resolved something",
                "path": "src/alpha.py",
            }
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    res = review(ctx, 8111, json.dumps(payload), "REQUEST_CHANGES")
    assert res == "Review submitted"
    assert mock_submit.called
    _submitted_pr, submitted_body, submitted_event, _target_key, _state = mock_submit.call_args[0]
    assert submitted_event == "REQUEST_CHANGES"
    submitted_data = json.loads(submitted_body)
    assert submitted_data["resolutions"][0]["status"] == "UNRESOLVED"
