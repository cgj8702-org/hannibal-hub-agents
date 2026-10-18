"""Unified event policy layer for WebhookAgent.

Replaces scattered, duplicated "is this a review event?" logic across five modules
with a single pure function: resolve_event_policy().

Every capability decision (can the bot submit a review? may it resolve threads?
may it create issues? prefetch diff? thinking budget?) flows through this one
function, making it the authoritative source of truth for what the agent may do
in response to any webhook event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("webhook_agent.core.policy")

# Review-intent keywords, synchronized with review/writeback_policy.py
REVIEW_INTENT_KEYWORDS: tuple[str, ...] = (
    "/review",
    "please review",
    "re-review",
    "review this",
    "request review",
    "run review",
    "audit this",
    "code review",
)

# Canonical PR lifecycle events that trigger formal review
FORMAL_REVIEW_EVENTS: frozenset[str] = frozenset(
    {
        "pull_request.opened",
        "pull_request.reopened",
        "pull_request.synchronize",
        "pull_request.ready_for_review",
        "pull_request_review_requested",
    }
)

# Events that carry a PR/issue context and may benefit from diff prefetch
PREFETCH_DIFF_EVENTS: frozenset[str] = frozenset(
    {
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.ready_for_review",
        "pull_request.reopened",
        "pull_request_review_requested",
    }
)


class Mode(str):
    """Operational mode for the WebhookAgent."""

    FORMAL_AUDIT = "formal_audit"  # PR lifecycle: clinical code review
    THREAD_COLLAB = "thread_collab"  # Inline review comment discussion
    ISSUE_TRIAGE = "issue_triage"  # Issue opened/commented: triage & delegate
    GENERAL_PAIR_PROGRAMMING = "general_pair_programming"  # General Q&A / chit-chat
    IGNORE = "ignore"  # Bot events, closed PRs, non-actionable events


@dataclass(frozen=True)
class EventPolicy:
    """Authoritative policy decision for a single webhook event.

    All fields are derived purely from the event data and dry_run flag.
    No I/O, no network, no GitHub API calls.
    """

    mode: Mode
    # Writeback permissions
    may_submit_review: bool = False
    may_post_comment: bool = False
    may_resolve_threads: bool = False
    may_create_issue: bool = False
    # Runtime configuration
    prefetch_diff: bool = False
    thinking_budget: int = 1024
    session_key: str = ""
    reason: str = ""


def resolve_event_policy(
    event_data: dict[str, Any],
    *,
    dry_run: bool = False,
) -> EventPolicy:
    """Single source of truth for event policy decisions.

    Pure function: same input always produces same output. No I/O, no network,
    no GitHub API calls. Replaces five scattered, duplicated implementations
    of "is this a review event?" across the codebase.

    Args:
        event_data: Normalized webhook event dictionary.
        dry_run: Whether the agent is in dry-run mode (suppresses mutations).

    Returns:
        EventPolicy describing what the agent may do for this event.
    """
    canonical = event_data.get("canonical", "")
    raw = event_data.get("raw_payload", {}) or {}
    comment_body = (raw.get("comment", {}) or {}).get("body", "")

    # --- IGNORE: bot events, non-actionable events ---
    sender = event_data.get("sender", {}) or {}
    sender_login = sender.get("login", "") if isinstance(sender, dict) else ""

    # Bot events are never actionable
    if sender_login.endswith("[bot]") or sender_login in (
        "coderabbitai",
        "coderabbitai[bot]",
        "google-labs-jules[bot]",
    ):
        return EventPolicy(
            mode=Mode.IGNORE,
            reason=f"bot event from {sender_login or 'unknown'}",
        )

    # --- FORMAL_AUDIT: PR lifecycle events ---
    if canonical in FORMAL_REVIEW_EVENTS:
        return EventPolicy(
            mode=Mode.FORMAL_AUDIT,
            may_submit_review=True,
            may_post_comment=True,
            prefetch_diff=True,
            thinking_budget=1024,
            session_key=f"{event_data.get('repository', {}).get('full_name', 'unknown')}#{_pr_number(raw)}",
            reason=f"formal review event: {canonical}",
        )

    # --- Review-intent comments on PRs/issues -> FORMAL_AUDIT or THREAD_COLLAB ---
    comment_text = comment_body.lower()
    if any(kw in comment_text for kw in REVIEW_INTENT_KEYWORDS):
        # Determine if this is a thread reply (thread context present) or general comment
        if _is_inline_review_comment(canonical, raw):
            return EventPolicy(
                mode=Mode.THREAD_COLLAB,
                may_post_comment=True,
                may_resolve_threads=True,
                prefetch_diff=True,
                thinking_budget=512,
                session_key=f"{event_data.get('repository', {}).get('full_name', 'unknown')}#{_pr_number(raw)}",
                reason=f"review thread collaboration: {canonical}",
            )
        return EventPolicy(
            mode=Mode.FORMAL_AUDIT,
            may_submit_review=True,
            may_post_comment=True,
            prefetch_diff=True,
            thinking_budget=1024,
            session_key=f"{event_data.get('repository', {}).get('full_name', 'unknown')}#{_pr_number(raw)}",
            reason=f"review intent: {canonical}",
        )

    # --- ISSUE_TRIAGE: issue events ---
    if canonical.startswith("issues."):
        return EventPolicy(
            mode=Mode.ISSUE_TRIAGE,
            may_post_comment=True,
            may_create_issue=True,
            thinking_budget=1024,
            session_key=f"{event_data.get('repository', {}).get('full_name', 'unknown')}#{_pr_number(raw)}",
            reason=f"issue event: {canonical}",
        )

    # --- GENERAL_PAIR_PROGRAMMING: general comments on PRs/issues ---
    if canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
        return EventPolicy(
            mode=Mode.GENERAL_PAIR_PROGRAMMING,
            may_post_comment=True,
            thinking_budget=512,
            session_key=f"{event_data.get('repository', {}).get('full_name', 'unknown')}#{_pr_number(raw)}",
            reason=f"general discussion: {canonical}",
        )

    # --- IGNORE: everything else ---
    return EventPolicy(
        mode=Mode.IGNORE,
        reason=f"unhandled event: {canonical}",
    )


def _pr_number(raw: dict[str, Any]) -> str:
    """Extract PR/issue number from raw payload."""
    pr = raw.get("pull_request") or {}
    if pr.get("number"):
        return str(pr["number"])
    issue = raw.get("issue") or {}
    if issue.get("number"):
        return str(issue["number"])
    return "unknown"


def _is_inline_review_comment(canonical: str, raw: dict[str, Any]) -> bool:
    """Check if this is an inline review comment thread (vs general comment)."""
    if not canonical.startswith("pull_request_review_comment."):
        return False
    # Inline review comments have a thread context; general comments don't
    comment = raw.get("comment", {}) or {}
    return bool(comment.get("thread_id") or comment.get("in_reply_to") or comment.get("body"))

