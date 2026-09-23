"""Proactive background evaluation service for open pull requests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from github import Github, GithubException

logger = logging.getLogger("webhook_agent.proactive")

STALE_THREAD_THRESHOLD_SECONDS = 24 * 3600  # 24 hours


class ProactiveEvaluator:
    """Evaluates repository state proactively without requiring user webhooks."""

    def __init__(self, gh: Github, repo_name: str) -> None:
        self.gh = gh
        self.repo_name = repo_name

    def evaluate_open_prs(self) -> list[dict[str, Any]]:
        """Scans open pull requests and performs proactive maintenance actions."""
        results: list[dict[str, Any]] = []
        try:
            repo = self.gh.get_repo(self.repo_name)
            open_prs = list(repo.get_pulls(state="open"))
            logger.info(
                "Proactive sweep: Scanning %d open PRs for %s",
                len(open_prs),
                self.repo_name,
            )

            for pr in open_prs:
                pr_result = self._evaluate_single_pr(pr)
                if pr_result:
                    results.append(pr_result)
        except GithubException:
            raise
        except Exception as exc:
            logger.error("Proactive sweep failed for repo %s: %s", self.repo_name, exc)

        return results

    def _evaluate_single_pr(self, pr: Any) -> dict[str, Any] | None:
        """Evaluate a PR for merge conflicts and stale review threads."""
        pr_number = pr.number
        actions_taken: list[str] = []

        # 1. Check Merge Conflicts
        # 1. Check Merge Conflicts
        is_dirty = getattr(pr, "mergeable_state", None) == "dirty"
        if getattr(pr, "mergeable", None) is False or is_dirty:
            if not self._has_recent_comment_with_text(
                pr, "Unable to automatically resolve merge conflicts"
            ):
                logger.info(
                    "Proactive Action: Detected merge conflict on PR #%d",
                    pr_number,
                )
                actions_taken.append("merge_conflict_detected")

        # 2. Check Stale Unresolved Threads (>24h)
        if self._has_stale_unresolved_thread(pr) and not self._has_recent_comment_with_text(
            pr, "Proactive Reminder: Unresolved Feedback"
        ):
            try:
                pr.create_issue_comment(
                    "## ⏰ Proactive Reminder: Unresolved Feedback\n\n"
                    "This PR has unresolved review feedback that has been idle for over 24 hours. "
                    "Please update the PR or reply to open threads when ready! 🚀\n\n"
                    "*Posted automatically by Hannibal Hub Proactive Agent*"
                )
                logger.info(
                    "Proactive Action: Posted stale thread reminder on PR #%d",
                    pr_number,
                )
                actions_taken.append("stale_thread_reminder_posted")
            except GithubException as exc:
                logger.warning(
                    "Failed to post stale thread reminder on PR #%d: %s",
                    pr_number,
                    exc,
                )

        if actions_taken:
            return {"pr_number": pr_number, "actions": actions_taken}
        return None

    def _has_stale_unresolved_thread(self, pr: Any) -> bool:
        """Check for inline review comments idle for more than 24 hours.

        Formal approvals and requested-change review records are not threads;
        only inline review comments can trigger this reminder.
        """
        try:
            get_review_comments = getattr(pr, "get_review_comments", None)
            review_comments = list(get_review_comments()) if callable(get_review_comments) else []
            if not review_comments:
                return False

            now = datetime.now(UTC)
            latest_comment_time = None
            for c in review_comments:
                c_time = getattr(c, "created_at", None) or getattr(c, "updated_at", None)
                if c_time:
                    if c_time.tzinfo is None:
                        c_time = c_time.replace(tzinfo=UTC)
                    if latest_comment_time is None or c_time > latest_comment_time:
                        latest_comment_time = c_time

            if not latest_comment_time:
                return False

            idle_seconds = (now - latest_comment_time).total_seconds()
            return idle_seconds > STALE_THREAD_THRESHOLD_SECONDS
        except Exception as exc:
            logger.debug(
                "Could not evaluate stale threads for PR #%d: %s",
                getattr(pr, "number", 0),
                exc,
            )
            return False

    def _has_recent_comment_with_text(self, pr: Any, substring: str) -> bool:
        """Checks if a bot comment containing the substring already exists on the PR."""
        try:
            comments = list(pr.get_issue_comments())
            for c in comments[-10:]:
                if substring in (c.body or ""):
                    return True
        except Exception:
            pass
        return False
