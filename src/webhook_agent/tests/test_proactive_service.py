"""Unit tests for ProactiveEvaluator service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from github import GithubException

from webhook_agent.proactive_service import ProactiveEvaluator

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


class TestProactiveEvaluator:
    def test_evaluate_open_prs_propagates_authentication_failure(self):
        mock_gh = MagicMock()
        mock_gh.get_repo.side_effect = GithubException(401, "Bad credentials", {})

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")

        with pytest.raises(GithubException) as exc_info:
            evaluator.evaluate_open_prs()

        assert exc_info.value.status == 401

    def test_evaluate_open_prs_empty(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_repo.get_pulls.return_value = []

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")
        results = evaluator.evaluate_open_prs()
        assert results == []

    def test_evaluate_stale_thread_reminder(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_pr.mergeable = True
        mock_pr.updated_at = datetime.now(UTC) - timedelta(hours=25)
        mock_comment = MagicMock()
        mock_comment.created_at = datetime.now(UTC) - timedelta(hours=25)
        mock_pr.get_review_comments.return_value = [mock_comment]
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 42
        assert "stale_thread_reminder_posted" in results[0]["actions"]
        mock_pr.create_issue_comment.assert_called_once()
        assert "Proactive Reminder" in mock_pr.create_issue_comment.call_args[0][0]

    def test_approval_without_inline_comments_does_not_trigger_reminder(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 158
        mock_pr.mergeable = True
        old_review = MagicMock()
        old_review.submitted_at = datetime.now(UTC) - timedelta(hours=25)
        mock_pr.get_review_comments.return_value = []
        mock_pr.get_reviews.return_value = [old_review]
        mock_pr.get_issue_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")

        assert evaluator.evaluate_open_prs() == []
        mock_pr.create_issue_comment.assert_not_called()
