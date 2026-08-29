"""Unit tests for ProactiveEvaluator service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from webhook_agent.proactive_service import ProactiveEvaluator


class TestProactiveEvaluator:
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
        mock_pr.updated_at = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_comment = MagicMock()
        mock_comment.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_pr.get_review_comments.return_value = [mock_comment]
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.head.sha = "abc1234"
        mock_repo.get_commit.return_value.get_check_runs.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 42
        assert "stale_thread_reminder_posted" in results[0]["actions"]
        mock_pr.create_issue_comment.assert_called_once()
        assert "Proactive Reminder" in mock_pr.create_issue_comment.call_args[0][0]

    def test_evaluate_failing_ci_check(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 99
        mock_pr.mergeable = True
        mock_pr.updated_at = datetime.now(timezone.utc)
        mock_pr.get_review_comments.return_value = []
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []

        mock_check = MagicMock()
        mock_check.name = "pytest"
        mock_check.conclusion = "failure"
        mock_repo.get_commit.return_value.get_check_runs.return_value = [mock_check]
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "owner/repo")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 99
        assert "ci_failure_diagnostic_posted" in results[0]["actions"]
        mock_pr.create_issue_comment.assert_called_once()
        assert "Failing CI Checks" in mock_pr.create_issue_comment.call_args[0][0]
