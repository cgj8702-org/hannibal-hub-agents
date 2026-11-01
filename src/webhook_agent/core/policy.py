"""Unified event policy layer for WebhookAgent.

Replaces scattered, duplicated "is this a review event?" logic across five modules
with a single pure function: resolve_event_policy().

Every capability decision (can the bot submit a review? may it resolve threads?
may it create issues? prefetch diff? thinking budget?) flows through this one
function, making it the authoritative source of truth for what the agent may do
in response to any webhook event.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from webhook_agent.core.commands import (
    has_review_slash_command,
)
from webhook_agent.github.bot_identity import is_hannibal_bot_login

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
        "cline-cloud[bot]",
        "cline-cloud",
    }
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
        "pull_request_review.submitted",
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
    is_explicit_request: bool = False


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
    comment_obj = raw.get("comment") or raw.get("review_comment") or {}
    comment_body = (comment_obj if isinstance(comment_obj, dict) else {}).get("body", "")
    comment_text = comment_body.lower()

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

    # 3. Comments directed to third-party bots (CodeRabbit, Dependabot) are ignored
    if any(bot in comment_text for bot in ("@coderabbitai", "@coderabbitai[bot]", "@dependabot")):
        return EventPolicy(
            mode=Mode.IGNORE,
            reason=f"comment targets third-party bot: {canonical}",
        )

    # Determine if this event involves a PR
    is_pr = bool(
        raw.get("pull_request") is not None
        or (raw.get("issue") or {}).get("pull_request") is not None
        or canonical.startswith(
            (
                "pull_request.",
                "pull_request_review.",
                "pull_request_review_comment.",
            )
        )
    )

    # --- FORMAL_AUDIT: PR lifecycle events ---
    # Strict guard: only PR events can enter formal audit mode.
    # Issue events must be triaged/discussed, never reviewed as PRs.
    if is_pr and canonical in FORMAL_REVIEW_EVENTS:
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

    is_pr = bool(
        raw.get("pull_request") is not None
        or (raw.get("issue") or {}).get("pull_request") is not None
        or canonical.startswith(
            (
                "pull_request.",
                "pull_request_review.",
                "pull_request_review_comment.",
            )
        )
    )

    # --- Review-intent slash commands on PRs -> FORMAL_AUDIT ---
    # Only PRs can enter formal audit; review commands on issues remain in triage/discussion
    if is_pr and has_review_slash_command(comment_body):
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
            is_explicit_request=True,
        )

    # 3. Non-PR bot activity (comments/reviews) is ignored unless review intent was requested.
    # Partner bots are exempt for ISSUE events only: issues filed by Cline were otherwise
    # never triaged (see #441). Triage cannot start a ping-pong loop, because any comment the
    # agent posts emits issue_comment — which this same rule still suppresses for bot senders.
    is_partner_bot = sender_login_lower in PARTNER_BOT_LOGINS
    partner_issue_event = is_partner_bot and canonical.startswith("issues.")
    if (sender_login_lower.endswith("[bot]") or is_partner_bot) and not partner_issue_event:
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
            may_create_issue=True,
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
