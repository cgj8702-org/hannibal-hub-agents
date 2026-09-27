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

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        from webhook_agent.proactive_service import _RECONCILED_PR_CACHE

        _RECONCILED_PR_CACHE.clear()
        yield
        _RECONCILED_PR_CACHE.clear()

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

    def test_unreviewed_pr_younger_than_5_minutes_is_skipped(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 101
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=3)
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_unreviewed_pr_older_than_5_minutes_dispatches_reconciliation(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 257
        mock_pr.title = "retire legacy archiver"
        mock_pr.body = "clean up old archiver scripts"
        mock_pr.html_url = "https://github.com/cgj8702-org/hannibal-hub/pull/257"
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=6)
        mock_pr.head.sha = "efe59f6c12345"
        mock_pr.head.ref = "agent/retire-legacy-archiver"
        mock_pr.base.sha = "main12345"
        mock_pr.base.ref = "main"
        mock_pr.user.login = "cgj8702"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 257
        assert "unreviewed_pr_reconciled" in results[0]["actions"]
        callback.assert_called_once()
        payload = callback.call_args[0][0]
        assert payload["canonical"] == "pull_request.opened"
        assert payload["raw_payload"]["pull_request"]["number"] == 257
        assert payload["raw_payload"]["pull_request"]["head"]["sha"] == "efe59f6c12345"

    def test_unreviewed_pr_older_than_5_minutes_without_callback_records_detection(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 257
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=6)
        mock_pr.head.sha = "efe59f6c12345"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "cgj8702-org/hannibal-hub")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 257
        assert "unreviewed_pr_detected" in results[0]["actions"]

    def test_unreviewed_pr_with_existing_bot_review_skipped(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 258
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=15)
        bot_review = MagicMock()
        bot_review.user.login = "hannibal-hub-agents[bot]"
        mock_pr.get_reviews.return_value = [bot_review]
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_unreviewed_pr_with_existing_bot_comment_skipped(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 258
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=15)
        bot_comment = MagicMock()
        bot_comment.user.login = "hannibal-hub-agents"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = [bot_comment]
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_unreviewed_pr_with_existing_eyes_reaction_skipped(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 258
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=15)
        reaction = MagicMock()
        reaction.content = "eyes"
        reaction.user.login = "hannibal-hub-agents[bot]"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = [reaction]
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_draft_pr_is_not_reconciled(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 259
        mock_pr.mergeable = True
        mock_pr.draft = True
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=10)
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_reconciled_pr_cache_prevents_duplicate_dispatch(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 257
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=6)
        mock_pr.head.sha = "efe59f6c12345"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_reactions.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )

        # First sweep triggers reconciliation
        first_results = evaluator.evaluate_open_prs()
        assert len(first_results) == 1
        assert "unreviewed_pr_reconciled" in first_results[0]["actions"]
        assert callback.call_count == 1

        # Second sweep is suppressed by cache
        second_results = evaluator.evaluate_open_prs()
        assert second_results == []
        assert callback.call_count == 1
