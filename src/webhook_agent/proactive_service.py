"""Proactive background evaluation service for open pull requests."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from github import Github, GithubException

logger = logging.getLogger("webhook_agent.proactive")

STALE_THREAD_THRESHOLD_SECONDS = 24 * 3600  # 24 hours
UNREVIEWED_PR_THRESHOLD_SECONDS = 300  # 5 minutes
RECONCILED_CACHE_TTL_SECONDS = 3600  # 1 hour

_RECONCILED_PR_CACHE: dict[str, float] = {}
_RECONCILIATION_LOCK = threading.Lock()


def try_claim_reconciliation(cache_key: str, now_ts: float | None = None) -> bool:
    """Atomically check and claim reconciliation for a PR head commit.

    Guards against concurrent thread execution across background sweeps and
    worker routines. Returns True if the claim was successfully acquired.
    """
    ts = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
    with _RECONCILIATION_LOCK:
        expired = [
            k
            for k, stored_ts in list(_RECONCILED_PR_CACHE.items())
            if ts - stored_ts > RECONCILED_CACHE_TTL_SECONDS
        ]
        for k in expired:
            _RECONCILED_PR_CACHE.pop(k, None)

        if cache_key in _RECONCILED_PR_CACHE:
            return False

        _RECONCILED_PR_CACHE[cache_key] = ts
        return True


def release_reconciliation_claim(cache_key: str) -> None:
    """Release a previously claimed reconciliation key, e.g. on execution failure."""
    with _RECONCILIATION_LOCK:
        _RECONCILED_PR_CACHE.pop(cache_key, None)


def is_reconciliation_claimed(cache_key: str, now_ts: float | None = None) -> bool:
    """Check if a reconciliation key is currently claimed without acquiring it."""
    ts = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
    with _RECONCILIATION_LOCK:
        if cache_key in _RECONCILED_PR_CACHE:
            if ts - _RECONCILED_PR_CACHE[cache_key] <= RECONCILED_CACHE_TTL_SECONDS:
                return True
            _RECONCILED_PR_CACHE.pop(cache_key, None)
        return False


def build_reconciliation_cache_key(repo_name: str, pr_number: int | str, head_sha: str) -> str:
    """Construct canonical reconciliation cache key."""
    return f"{repo_name}#{pr_number}#{head_sha}"


def clear_reconciliation_cache() -> None:
    """Atomically reset the reconciliation cache (used primarily for test isolation)."""
    with _RECONCILIATION_LOCK:
        _RECONCILED_PR_CACHE.clear()


def build_synthetic_pr_opened_event(pr: Any, repo_name: str) -> dict[str, Any]:
    """Build a normalized pull_request.opened payload for unreviewed PR reconciliation."""
    head = getattr(pr, "head", None)
    head_sha = getattr(head, "sha", "") or ""
    head_ref = getattr(head, "ref", "") or ""

    base = getattr(pr, "base", None)
    base_ref = getattr(base, "ref", "main") or "main"
    base_sha = getattr(base, "sha", "") or ""

    user = getattr(pr, "user", None)
    user_login = getattr(user, "login", "unknown") or "unknown"

    pr_number = getattr(pr, "number", 0)
    pr_title = getattr(pr, "title", "") or ""
    pr_body = getattr(pr, "body", "") or ""
    html_url = getattr(pr, "html_url", "") or f"https://github.com/{repo_name}/pull/{pr_number}"

    owner_login = repo_name.split("/")[0] if "/" in repo_name else "unknown"
    repo_short_name = repo_name.split("/")[-1] if "/" in repo_name else repo_name

    repo_dict = {
        "full_name": repo_name,
        "name": repo_short_name,
        "owner": {"login": owner_login},
    }

    return {
        "event_name": "pull_request",
        "action": "opened",
        "canonical": "pull_request.opened",
        "delivery_id": f"proactive-reconcile-{pr_number}-{head_sha[:7]}",
        "repository": repo_dict,
        "sender": {
            "login": user_login,
            "type": "User",
        },
        "raw_payload": {
            "action": "opened",
            "number": pr_number,
            "pull_request": {
                "number": pr_number,
                "title": pr_title,
                "body": pr_body,
                "state": "open",
                "html_url": html_url,
                "user": {"login": user_login},
                "head": {
                    "ref": head_ref,
                    "sha": head_sha,
                },
                "base": {
                    "ref": base_ref,
                    "sha": base_sha,
                },
            },
            "repository": repo_dict,
            "sender": {
                "login": user_login,
            },
        },
    }


class ProactiveEvaluator:
    """Evaluates repository state proactively without requiring user webhooks."""

    def __init__(
        self,
        gh: Github,
        repo_name: str,
        on_unreviewed_pr: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.gh = gh
        self.repo_name = repo_name
        self.on_unreviewed_pr = on_unreviewed_pr

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
        """Evaluate a PR for unreviewed status, merge conflicts, and stale review threads."""
        pr_number = pr.number
        actions_taken: list[str] = []
        now = datetime.now(UTC)

        # 1. Check Unreviewed PR Reconciliation (>= 5 mins old)
        if self._is_unreviewed_and_mature(pr, now):
            head = getattr(pr, "head", None)
            head_sha = getattr(head, "sha", "") or ""
            cache_key = build_reconciliation_cache_key(self.repo_name, pr_number, head_sha)
            now_ts = now.timestamp()
            if try_claim_reconciliation(cache_key, now_ts):
                event_payload = build_synthetic_pr_opened_event(pr, self.repo_name)
                if callable(self.on_unreviewed_pr):
                    try:
                        self.on_unreviewed_pr(event_payload)
                        logger.info(
                            "Proactive Action: Reconciled unreviewed PR #%d for %s",
                            pr_number,
                            self.repo_name,
                        )
                        actions_taken.append("unreviewed_pr_reconciled")
                    except Exception as exc:
                        logger.error(
                            "Failed to execute on_unreviewed_pr callback for PR #%d: %s",
                            pr_number,
                            exc,
                        )
                else:
                    logger.info(
                        "Proactive Action: Detected unreviewed PR #%d for %s",
                        pr_number,
                        self.repo_name,
                    )
                    actions_taken.append("unreviewed_pr_detected")

        # 2. Check Merge Conflicts
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

        # 3. Check Stale Unresolved Threads (>24h)
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

    def _has_bot_review_or_comment(self, pr: Any) -> bool:
        """Check if the bot has already submitted a review or posted an issue comment."""
        try:
            get_reviews = getattr(pr, "get_reviews", None)
            reviews = list(get_reviews()) if callable(get_reviews) else []
            for r in reviews:
                user = getattr(r, "user", None)
                login = (getattr(user, "login", "") or "").lower()
                if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                    return True

            get_comments = getattr(pr, "get_issue_comments", None)
            comments = list(get_comments()) if callable(get_comments) else []
            for c in comments:
                user = getattr(c, "user", None)
                login = (getattr(user, "login", "") or "").lower()
                if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                    return True
        except Exception as exc:
            logger.debug(
                "Could not evaluate bot review status for PR #%d: %s",
                getattr(pr, "number", 0),
                exc,
            )
        return False

    def _has_bot_eyes_reaction(self, pr: Any) -> bool:
        """Check if the bot has already acknowledged the PR with an eyes reaction."""
        try:
            get_reactions = getattr(pr, "get_reactions", None)
            reactions = list(get_reactions()) if callable(get_reactions) else []
            for r in reactions:
                content = getattr(r, "content", "")
                user = getattr(r, "user", None)
                login = (getattr(user, "login", "") or "").lower()
                if content == "eyes" and (
                    "hannibal-hub-agents" in login or login.endswith("[bot]")
                ):
                    return True
        except Exception:
            pass
        return False

    def _is_unreviewed_and_mature(self, pr: Any, now: datetime) -> bool:
        """Check if an open PR is mature (>= 5 minutes old) and completely unreviewed."""
        if getattr(pr, "draft", False) is True:
            return False

        created_at = getattr(pr, "created_at", None)
        if not isinstance(created_at, datetime):
            return False

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        age_seconds = (now - created_at).total_seconds()
        if age_seconds < UNREVIEWED_PR_THRESHOLD_SECONDS:
            return False

        return not (self._has_bot_review_or_comment(pr) or self._has_bot_eyes_reaction(pr))

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
