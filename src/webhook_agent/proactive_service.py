"""Proactive background evaluation service for open pull requests."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
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
                pr_result = self._evaluate_single_pr(repo, pr)
                if pr_result:
                    results.append(pr_result)
        except Exception as exc:
            logger.error("Proactive sweep failed for repo %s: %s", self.repo_name, exc)

        return results

    def _evaluate_single_pr(self, repo: Any, pr: Any) -> dict[str, Any] | None:
        """Evaluates a single PR for stale threads, merge conflicts, and failing CI."""
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
        if self._has_stale_unresolved_thread(pr):
            if not self._has_recent_comment_with_text(
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

        # 3. Check Failing CI Check Runs
        failing_checks = self._get_failing_check_runs(pr)
        if failing_checks:
            if not self._has_recent_comment_with_text(
                pr, "Proactive Diagnostic: Failing CI Checks"
            ):
                try:
                    checks_summary = "\n".join(
                        f"- ❌ **{c['name']}**: `{c['conclusion']}`"
                        for c in failing_checks
                    )
                    pr.create_issue_comment(
                        f"## 🚨 Proactive Diagnostic: Failing CI Checks\n\n"
                        f"The following CI checks failed on this PR:\n{checks_summary}\n\n"
                        f"Please review the check run details or run `uv run pytest` / `bash scripts/ruff-all.sh` locally to fix. 🛠️\n\n"
                        f"*Posted automatically by Hannibal Hub Proactive Agent*"
                    )
                    logger.info(
                        "Proactive Action: Posted CI failure diagnostic on PR #%d",
                        pr_number,
                    )
                    actions_taken.append("ci_failure_diagnostic_posted")
                except GithubException as exc:
                    logger.warning(
                        "Failed to post CI failure diagnostic on PR #%d: %s",
                        pr_number,
                        exc,
                    )

        if actions_taken:
            return {"pr_number": pr_number, "actions": actions_taken}
        return None

    def _has_stale_unresolved_thread(self, pr: Any) -> bool:
        """Checks if PR has actual review comments >24h old with no subsequent activity."""
        try:
            get_review_comments = getattr(pr, "get_review_comments", None)
            review_comments = (
                list(get_review_comments()) if callable(get_review_comments) else []
            )
            get_reviews = getattr(pr, "get_reviews", None)
            reviews = list(get_reviews()) if callable(get_reviews) else []
            if not review_comments and not reviews:
                return False

            now = datetime.now(timezone.utc)
            latest_comment_time = None
            for c in review_comments:
                c_time = getattr(c, "created_at", None) or getattr(
                    c, "updated_at", None
                )
                if c_time:
                    if c_time.tzinfo is None:
                        c_time = c_time.replace(tzinfo=timezone.utc)
                    if latest_comment_time is None or c_time > latest_comment_time:
                        latest_comment_time = c_time

            for r in reviews:
                r_time = getattr(r, "submitted_at", None)
                if r_time:
                    if r_time.tzinfo is None:
                        r_time = r_time.replace(tzinfo=timezone.utc)
                    if latest_comment_time is None or r_time > latest_comment_time:
                        latest_comment_time = r_time

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

    def _get_failing_check_runs(self, pr: Any) -> list[dict[str, str]]:
        """Retrieves failing GitHub Actions check runs for the PR's head commit."""
        failing: list[dict[str, str]] = []
        try:
            head_sha = getattr(getattr(pr, "head", None), "sha", None)
            if not head_sha:
                return failing
            repo = self.gh.get_repo(self.repo_name)
            commit = repo.get_commit(head_sha)
            check_runs = commit.get_check_runs()
            for check in check_runs:
                conclusion = check.conclusion
                if conclusion in ("failure", "timed_out", "action_required"):
                    failing.append(
                        {
                            "name": check.name or "CI Check",
                            "conclusion": conclusion,
                        }
                    )
        except Exception as exc:
            logger.debug(
                "Could not fetch check runs for PR #%d: %s",
                getattr(pr, "number", 0),
                exc,
            )
        return failing
