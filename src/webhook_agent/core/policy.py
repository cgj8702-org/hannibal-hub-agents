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
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from webhook_agent.github.bot_identity import is_hannibal_bot_login

logger = logging.getLogger("webhook_agent.core.policy")

# Bot logins that are never actionable (own bot loop avoidance or automated reviewers)
SUPPRESSED_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "coderabbitai",
        "coderabbitai[bot]",
        "github-actions[bot]",
        "github-actions",
    }
)

# Autonomous partner coding bots whose PRs are subject to zero-bypass code review
PARTNER_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "google-labs-jules[bot]",
        "google-labs-jules",
        "google-jules[bot]",
        "google-jules",
        "cline-cloud[bot]",
        "cline-cloud",
    }
)

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

DEFAULT_AUDITOR_THINKING_BUDGET = 4096


def get_auditor_thinking_budget() -> int:
    """Return configured thinking budget for the code auditor, falling back to 4096."""
    raw = os.environ.get("AUDITOR_THINKING_BUDGET", "").strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_AUDITOR_THINKING_BUDGET


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


class Mode(StrEnum):
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
    sender_login = sender.get("login", "") if isinstance(sender, dict) else str(sender or "")
    sender_login_lower = sender_login.lower().strip()

    # 1. Own bot events are never actionable (loop avoidance)
    if is_hannibal_bot_login(sender_login_lower):
        return EventPolicy(
            mode=Mode.IGNORE,
            reason=f"own-bot event from {sender_login or 'unknown'}",
        )

    # 2. Suppressed automated reviewer bots are never actionable
    if sender_login_lower in SUPPRESSED_BOT_LOGINS:
        return EventPolicy(
            mode=Mode.IGNORE,
            reason=f"suppressed bot event from {sender_login or 'unknown'}",
        )

    # --- FORMAL_AUDIT: PR lifecycle events ---
    # Partner bots (Cline, Jules) and human contributors get zero-bypass formal audit on PR events
    if canonical in FORMAL_REVIEW_EVENTS:
        session_key = _derive_session_key(event_data, raw, canonical)
        auditor_budget = get_auditor_thinking_budget()
        return EventPolicy(
            mode=Mode.FORMAL_AUDIT,
            may_submit_review=True,
            may_post_comment=True,
            prefetch_diff=True,
            thinking_budget=auditor_budget,
            session_key=session_key,
            reason=f"formal review event: {canonical}",
        )

    # --- Review-intent comments on PRs/issues -> FORMAL_AUDIT ---
    comment_text = comment_body.lower()
    if any(kw in comment_text for kw in REVIEW_INTENT_KEYWORDS):
        session_key = _derive_session_key(event_data, raw, canonical)
        auditor_budget = get_auditor_thinking_budget()
        return EventPolicy(
            mode=Mode.FORMAL_AUDIT,
            may_submit_review=True,
            may_post_comment=True,
            prefetch_diff=True,
            thinking_budget=auditor_budget,
            session_key=session_key,
            reason=f"review intent: {canonical}",
        )

    # 3. Non-PR bot activity (comments/reviews) is ignored unless review intent was requested
    if sender_login_lower.endswith("[bot]") or sender_login_lower in PARTNER_BOT_LOGINS:
        return EventPolicy(
            mode=Mode.IGNORE,
            reason=f"unprompted bot comment/activity from {sender_login or 'unknown'}",
        )

    # --- THREAD_COLLAB: inline review comment events from humans ---
    if canonical.startswith("pull_request_review_comment."):
        session_key = _derive_session_key(event_data, raw, canonical)
        return EventPolicy(
            mode=Mode.THREAD_COLLAB,
            may_post_comment=True,
            may_resolve_threads=True,
            prefetch_diff=True,
            thinking_budget=512,
            session_key=session_key,
            reason=f"review thread collaboration: {canonical}",
        )

    # --- ISSUE_TRIAGE: issue events ---
    if canonical.startswith("issues."):
        session_key = _derive_session_key(event_data, raw, canonical)
        return EventPolicy(
            mode=Mode.ISSUE_TRIAGE,
            may_post_comment=True,
            may_create_issue=True,
            thinking_budget=1024,
            session_key=session_key,
            reason=f"issue event: {canonical}",
        )

    # --- GENERAL_PAIR_PROGRAMMING: general comments on PRs/issues ---
    if canonical.startswith("issue_comment."):
        session_key = _derive_session_key(event_data, raw, canonical)
        return EventPolicy(
            mode=Mode.GENERAL_PAIR_PROGRAMMING,
            may_post_comment=True,
            thinking_budget=512,
            session_key=session_key,
            reason=f"general discussion: {canonical}",
        )

    # --- IGNORE: everything else ---
    return EventPolicy(
        mode=Mode.IGNORE,
        reason=f"unhandled event: {canonical}",
    )


def _extract_issue_or_pr_number(raw: dict[str, Any]) -> int | None:
    """Extract PR or issue number from raw payload."""
    if not isinstance(raw, dict):
        return None
    pr = raw.get("pull_request")
    if isinstance(pr, dict) and pr.get("number"):
        return int(pr["number"])
    issue = raw.get("issue")
    if isinstance(issue, dict) and issue.get("number"):
        return int(issue["number"])
    return None


def _derive_session_key(event_data: dict[str, Any], raw: dict[str, Any], canonical: str) -> str:
    """Derive PR-scoped session key (owner/repo#42) for actionable events. Fast-fails without fallback."""
    repo = event_data.get("repository") or {}
    repo_name = repo.get("full_name") if isinstance(repo, dict) else None
    if not repo_name or repo_name == "unknown":
        repo_name = event_data.get("repo_name")
    if not repo_name or repo_name == "unknown":
        raise ValueError(
            f"Cannot resolve event policy: actionable event '{canonical}' is missing repository full_name"
        )

    number = _extract_issue_or_pr_number(raw)
    if not number:
        raise ValueError(
            f"Cannot resolve event policy: actionable event '{canonical}' for repository '{repo_name}' is missing PR/issue number"
        )
    return f"{repo_name}#{number}"
