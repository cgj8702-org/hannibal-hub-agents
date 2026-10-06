"""Pre-fetching and context enhancement for GitHub pull request webhook events.

Extracted from processor.py as part of Phase 3d modularization.
"""

from __future__ import annotations

import logging
from typing import Any

from github import Github

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.cancellation import pr_closed_registry
from webhook_agent.review.metadata import (
    get_actionable_findings,
    has_actionable_findings,
)
from webhook_agent.review.writeback_policy import REVIEW_INTENT_KEYWORDS

logger = logging.getLogger("webhook_agent.pr_context")


def _should_prefetch_diff(canonical: str, raw: dict[str, Any]) -> bool:
    """Determine if a PR diff pre-fetch is necessary for this event to avoid prompt bloat.

    Pre-fetching is restricted to PR creation/updates, review requests, and explicit
    review intent triggers.
    """
    if canonical in (
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.ready_for_review",
        "pull_request.reopened",
        "pull_request_review_requested",
    ):
        return True

    if canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
        comment_body = (raw.get("comment", {}) or {}).get("body", "").lower()
        if any(trigger in comment_body for trigger in REVIEW_INTENT_KEYWORDS):
            return True
        # Forward-fix: issue_comment on a PR (issue.pull_request set) that needs
        # reconciliation against prior bot reviews should also get diff context.
        issue = raw.get("issue") or {}
        if isinstance(issue, dict) and issue.get("pull_request") is not None:
            return True

    return False


def _prefetch_pr_diff(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Programmatically pre-fetch PR diff and inject into raw_payload for 1-turn review execution."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "pr_diff" in raw:
            return

        if not _should_prefetch_diff(canonical, raw):
            return

        pr_number = None
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")
        elif "issue" in raw and isinstance(raw["issue"], dict) and raw["issue"].get("pull_request"):
            pr_number = raw["issue"].get("number")

        if pr_number is None:
            return

        try:
            pr_num_int = int(pr_number)
        except (TypeError, ValueError):
            return

        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_num_int)

        # Check live PR state: if closed or merged, register in pr_closed_registry
        raw_state = getattr(pr, "state", None)
        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        is_merged = getattr(pr, "merged", None) is True
        if pr_state == "closed" or is_merged:
            pr_closed_registry.mark_closed(repo_name, pr_num_int)
            if "pull_request" in raw and isinstance(raw["pull_request"], dict):
                raw["pull_request"]["state"] = "closed"
                raw["pull_request"]["merged"] = True
            elif "issue" in raw and isinstance(raw["issue"], dict):
                raw["issue"]["state"] = "closed"
            logger.info(
                "🔒 Live GitHub check: PR %s#%d is closed or merged (state=%s, merged=%s); registered as closed",
                repo_name,
                pr_num_int,
                pr_state,
                is_merged,
            )
            return

        changed_files: list[str] = []
        diff_lines: list[str] = []
        for f in pr.get_files():
            changed_files.append(f.filename)
            patch = f.patch or "No patch available (binary/renamed/empty)."
            diff_lines.append(f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}")

        if diff_lines:
            raw["changed_files"] = changed_files
            raw_diff = "\n".join(diff_lines)
            filtered = filter_review_diff(raw_diff)
            raw["pr_diff"] = filtered.filtered_diff or raw_diff
            logger.info(
                "Pre-fetched PR #%d diff (%d files, %d kept, churn=%d) for 1-turn review",
                pr_num_int,
                len(diff_lines),
                len(filtered.kept_files),
                filtered.reviewable_lines,
            )

        if canonical == "pull_request.synchronize" or raw.get("action") == "synchronize":
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

        _prefetch_inline_comment_context(gh, repo_name, payload)

    except Exception as exc:
        logger.debug("Could not pre-fetch PR diff: %s", exc)


def _prefetch_inline_comment_context(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Pre-fetch code context snippet for inline review comment events."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload")
        if (
            not isinstance(raw, dict)
            or not canonical.startswith("pull_request_review_comment.")
            or "inline_code_context" in raw
        ):
            return

        comment = raw.get("comment", {})
        path = comment.get("path")
        diff_hunk = comment.get("diff_hunk")
        line = comment.get("line") or comment.get("original_line")

        if path and (diff_hunk or line):
            raw["inline_code_context"] = (
                f"File: {path} (Line {line})\nDiff Hunk Snippet:\n{diff_hunk or 'N/A'}"
            )
            logger.info("Pre-fetched inline comment code context for %s:%s", path, line)
    except Exception as exc:
        logger.debug("Could not pre-fetch inline comment context: %s", exc)


def _prefetch_previous_bot_reviews(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Pre-fetch previous reviews posted by hannibal-hub-agents[bot]."""
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "previous_bot_reviews" in raw:
            return

        pr_number = None
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")
        elif (
            "issue" in raw
            and isinstance(raw["issue"], dict)
            and raw["issue"].get("pull_request") is not None
        ):
            pr_number = raw["issue"].get("number")

        if not pr_number:
            return

        repo = gh.get_repo(repo_name)
        try:
            pr = repo.get_pull(pr_number)
        except Exception:
            return

        bot_reviews: list[str] = []
        had_request_changes = False
        had_prior_findings = False
        all_prior_findings: list[dict[str, Any]] = []

        for r in pr.get_reviews():
            u = getattr(r, "user", None)
            login = (getattr(u, "login", "") or "").lower() if u else ""
            if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                state = getattr(r, "state", "COMMENT")
                if state == "CHANGES_REQUESTED":
                    had_request_changes = True
                body = (r.body or "").strip()
                if has_actionable_findings(body, state):
                    had_prior_findings = True
                findings = get_actionable_findings(body)
                if findings:
                    all_prior_findings.extend(findings)
                body_clean = body
                bot_reviews.append(f"Review (State: {state}):\n{body_clean}")

        raw["prior_reviews_had_request_changes"] = had_request_changes
        raw["prior_reviews_had_findings"] = had_prior_findings or had_request_changes
        raw["prior_actionable_findings"] = all_prior_findings
        if bot_reviews:
            raw["previous_bot_reviews"] = "\n\n---\n\n".join(bot_reviews)
            logger.info(
                "Pre-fetched previous bot reviews (%d reviews, had_request_changes=%s, had_findings=%s, findings_count=%d) for PR #%d",
                len(bot_reviews),
                had_request_changes,
                raw["prior_reviews_had_findings"],
                len(all_prior_findings),
                pr_number,
            )

    except Exception as exc:
        logger.debug("Could not pre-fetch previous bot reviews: %s", exc)


__all__ = [
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_should_prefetch_diff",
]
