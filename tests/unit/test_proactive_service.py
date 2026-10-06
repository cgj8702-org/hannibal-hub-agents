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
        from webhook_agent.proactive_service import clear_reconciliation_cache

        clear_reconciliation_cache()
        yield
        clear_reconciliation_cache()

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

    def test_merge_conflict_detected_on_unmergeable_pr(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 101
        mock_pr.mergeable = False
        mock_pr.mergeable_state = "clean"
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_review_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "cgj8702-org/hannibal-hub")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 101
        assert "merge_conflict_detected" in results[0]["actions"]

    def test_merge_conflict_detected_on_dirty_state(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 102
        mock_pr.mergeable = True
        mock_pr.mergeable_state = "dirty"
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_review_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "cgj8702-org/hannibal-hub")
        results = evaluator.evaluate_open_prs()

        assert len(results) == 1
        assert results[0]["pr_number"] == 102
        assert "merge_conflict_detected" in results[0]["actions"]

    def test_merge_conflict_skipped_if_already_commented(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 103
        mock_pr.mergeable = False
        mock_comment = MagicMock()
        mock_comment.body = "Unable to automatically resolve merge conflicts for PR #103."
        mock_pr.get_issue_comments.return_value = [mock_comment]
        mock_pr.get_review_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        evaluator = ProactiveEvaluator(mock_gh, "cgj8702-org/hannibal-hub")
        results = evaluator.evaluate_open_prs()

        assert results == []

    def test_unreviewed_pr_does_not_dispatch_synthetic_events_or_llms(self):
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_pr.number = 257
        mock_pr.mergeable = True
        mock_pr.mergeable_state = "clean"
        mock_pr.draft = False
        mock_pr.created_at = datetime.now(UTC) - timedelta(minutes=6)
        mock_pr.head.sha = "efe59f6c12345"
        mock_pr.get_reviews.return_value = []
        mock_pr.get_issue_comments.return_value = []
        mock_pr.get_review_comments.return_value = []
        mock_repo.get_pulls.return_value = [mock_pr]

        callback = MagicMock()
        evaluator = ProactiveEvaluator(
            mock_gh, "cgj8702-org/hannibal-hub", on_unreviewed_pr=callback
        )
        results = evaluator.evaluate_open_prs()

        assert results == []
        callback.assert_not_called()

    def test_concurrent_threads_reconciliation_claim_is_thread_safe(self):
        import concurrent.futures

        from webhook_agent.proactive_service import try_claim_reconciliation

        cache_key = "cgj8702-org/hannibal-hub#300#abcdef1"
        now_ts = 1000000.0
        results: list[bool] = []

        def worker():
            return try_claim_reconciliation(cache_key, now_ts)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(20)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        # Exactly 1 thread successfully claimed the reconciliation slot
        assert results.count(True) == 1
        assert results.count(False) == 19

    def test_release_reconciliation_claim_allows_reclaim(self):
        from webhook_agent.proactive_service import (
            release_reconciliation_claim,
            try_claim_reconciliation,
        )

        key = "cgj8702-org/hannibal-hub#301#1122334"
        assert try_claim_reconciliation(key) is True
        assert try_claim_reconciliation(key) is False

        release_reconciliation_claim(key)
        assert try_claim_reconciliation(key) is True

    def test_is_reconciliation_claimed_checks_without_acquiring(self):
        from webhook_agent.proactive_service import (
            is_reconciliation_claimed,
            try_claim_reconciliation,
        )

        key = "cgj8702-org/hannibal-hub#302#aabbcc"
        assert is_reconciliation_claimed(key) is False
        assert try_claim_reconciliation(key) is True
        assert is_reconciliation_claimed(key) is True

    def test_build_reconciliation_cache_key_format(self):
        from webhook_agent.proactive_service import build_reconciliation_cache_key

        assert (
            build_reconciliation_cache_key("owner/repo", 42, "deadbeef") == "owner/repo#42#deadbeef"
        )

    def test_webhook_in_flight_claims_deduplication(self):
        from webhook_agent.proactive_service import (
            build_reconciliation_cache_key,
            release_reconciliation_claim,
            try_claim_reconciliation,
        )

        key = build_reconciliation_cache_key("cgj8702-org/hannibal-hub", 299, "webhooksha123")
        assert try_claim_reconciliation(key) is True
        # Duplicate incoming delivery for the same PR commit fails claim
        assert try_claim_reconciliation(key) is False

        # After releasing claim, a retry can acquire the claim
        release_reconciliation_claim(key)
        assert try_claim_reconciliation(key) is True
