"""Unit tests for core.execution execution runner and writeback dispatch."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.core.execution import (
    _post_conversational_reply,
    _submit_deterministic_review,
    execute_agent_event,
)
from webhook_agent.webhook_types import ActionResult

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_post_conversational_reply_success() -> None:
    """Test _post_conversational_reply successfully creates a comment on the issue."""
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_issue = mock_repo.get_issue.return_value
    mock_comment = MagicMock()
    mock_comment.html_url = "https://github.com/owner/repo/issues/10#issuecomment-1"
    mock_issue.create_comment.return_value = mock_comment

    results: list[ActionResult] = []
    with (
        patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.is_allowed", return_value=True),
        patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.record") as mock_record,
    ):
        _post_conversational_reply(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=10,
            emitted_texts=["First thought", "Hello author!"],
            trace_id="trace-1234",
            results=results,
        )

        mock_issue.create_comment.assert_called_once_with("First thought\n\nHello author!")
        mock_record.assert_called_once_with("owner/repo#10")
        assert len(results) == 1
        assert results[0].tool == "add_comment"
        assert results[0].success is True


def test_post_conversational_reply_rate_limited() -> None:
    """Test _post_conversational_reply skips comment creation when rate limited."""
    mock_gh = MagicMock()
    results: list[ActionResult] = []
    with patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.is_allowed", return_value=False):
        _post_conversational_reply(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=10,
            emitted_texts=["Hello!"],
            trace_id="trace-1234",
            results=results,
        )

        mock_gh.get_repo.assert_not_called()
        assert len(results) == 0


def test_submit_deterministic_review_with_session_state() -> None:
    """Test _submit_deterministic_review extracts payload from session state and submits review."""
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = mock_repo.get_pull.return_value

    final_session = MagicMock()
    final_session.state = {
        "code_review_analysis": {"verdict": "APPROVE", "executive_summary": "Solid design."}
    }

    results: list[ActionResult] = []
    with (
        patch(
            "webhook_agent.core.execution._submit_formal_review",
            return_value=("Formal review submitted", True),
        ) as mock_submit,
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.mark_completed"
        ) as mock_checkpoint,
    ):
        _submit_deterministic_review(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=42,
            head_sha="commit-sha-123",
            canonical="pull_request.opened",
            final_session=final_session,
            emitted_texts=[],
            results=results,
        )

        mock_submit.assert_called_once()
        mock_checkpoint.assert_called_once_with("owner/repo", 42, "commit-sha-123")
        assert len(results) == 1
        assert results[0].tool == "review"
        assert results[0].success is True


def test_execute_agent_event_checkpoint_short_circuit() -> None:
    """Test execute_agent_event short-circuits when commit checkpoint is already completed."""
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._build_user_message.return_value = MagicMock(parts=[MagicMock(text="Audit request")])
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    mock_gh = MagicMock()
    with patch(
        "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
        return_value={"status": "completed"},
    ):
        results = execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-test",
        )

        assert len(results) == 1
        assert results[0].tool == "review"
        assert results[0].success is True
        assert "already reviewed" in results[0].detail
