"""Writeback policy evaluation, sliding-window rate limiting, and PR locks.

Extracted from webhook_agent.py as Phase 2 of codebase modularization.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from webhook_agent.constants import DEFAULT_ALLOW_AUTOMATED_MUTATIONS
from webhook_agent.webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.writeback_policy")


class CommentRateLimiter:
    """Sliding window rate limiter to prevent comment spam per issue/PR."""

    def __init__(self, max_comments: int = 3, window_seconds: float = 60.0) -> None:
        self.max_comments = max_comments
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, target_key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        timestamps = [t for t in self._history.get(target_key, []) if t > cutoff]
        self._history[target_key] = timestamps
        return len(timestamps) < self.max_comments

    def record(self, target_key: str) -> None:
        now = time.time()
        if target_key not in self._history:
            self._history[target_key] = []
        self._history[target_key].append(now)


_COMMENT_RATE_LIMITER = CommentRateLimiter(max_comments=3, window_seconds=60.0)
_REVIEW_LOCKS: dict[str, threading.Lock] = {}
_REVIEW_LOCKS_GUARD = threading.Lock()


def _is_formal_review_eligible(canonical: str, comment_body: str = "") -> bool:
    """Return whether an event is allowed to initiate a formal code review."""
    return (
        canonical
        in {
            "pull_request.opened",
            "pull_request.reopened",
            "pull_request.synchronize",
            "pull_request_review_requested",
        }
        or "/review" in comment_body.lower()
    )


def _review_lock(target_key: str) -> threading.Lock:
    """Return the process-local lock used to serialize one PR's submissions."""
    with _REVIEW_LOCKS_GUARD:
        return _REVIEW_LOCKS.setdefault(target_key, threading.Lock())


def evaluate_writeback_policy(
    event_data: dict[str, Any],
    dry_run: bool,
    trace_id: str,
    is_bot_event_fn: Any = None,
) -> list[ActionResult] | None:
    """Evaluate repository mutation and writeback policy before agent execution.

    Args:
        event_data: Normalized webhook event dictionary.
        dry_run: Whether the agent is configured in dry-run mode.
        trace_id: Correlation trace identifier for logging.
        is_bot_event_fn: Optional callable to check if event is bot-originated.

    Returns:
        List of ActionResult objects if policy blocks or short-circuits execution,
        or None if all writeback policy checks pass and execution should proceed.
    """
    canonical = event_data.get("canonical", "")

    logger.debug(
        "🔍 Checking writeback policy: canonical=%s, dry_run=%s, trace=%s",
        canonical,
        dry_run,
        trace_id[-4:] if len(trace_id) >= 4 else trace_id,
    )

    if is_bot_event_fn is not None and is_bot_event_fn(event_data):
        sender = event_data.get("sender", {})
        logger.debug(
            "🤖 Bot event detected: sender=%s, canonical=%s",
            sender.get("login", "unknown"),
            canonical,
        )
        logger.info(
            "writeback blocked: bot-originated event '%s' (trace: %s)",
            canonical,
            trace_id[-4:] if len(trace_id) >= 4 else trace_id,
        )
        return [
            ActionResult(
                tool="plan",
                success=False,
                detail=f"writeback policy: bot-originated event '{canonical}' blocked",
            )
        ]

    # Check read-only events
    read_only_events: set[str] = {
        "ping",
        "unknown",
    }
    if canonical in read_only_events:
        logger.debug(
            "📖 Read-only event detected: canonical=%s",
            canonical,
        )
        logger.info(
            "writeback policy: event '%s' is read-only (trace: %s)",
            canonical,
            trace_id[-4:] if len(trace_id) >= 4 else trace_id,
        )
        return [
            ActionResult(
                tool="plan",
                success=False,
                detail=f"writeback policy: event '{canonical}' is read-only",
            )
        ]

    # Short-circuit execution if PR is closed or merged
    raw = event_data.get("raw_payload") or {}
    pr_data = raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
    if isinstance(raw.get("issue"), dict) and not pr_data:
        pr_data = raw.get("issue") or {}

    repo_name = event_data.get("repo_name") or (raw.get("repository") or {}).get("full_name") or ""
    pr_number = pr_data.get("number") if isinstance(pr_data, dict) else None

    try:
        from webhook_agent.cancellation import pr_closed_registry
    except ImportError:
        pr_closed_registry = None  # type: ignore[assignment]

    is_registry_closed = bool(
        repo_name
        and pr_number
        and pr_closed_registry
        and pr_closed_registry.is_closed(repo_name, int(pr_number))
    )

    raw_state = pr_data.get("state") if isinstance(pr_data, dict) else ""
    pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
    merged_at_val = pr_data.get("merged_at") if isinstance(pr_data, dict) else None
    is_merged = (
        pr_data.get("merged") is True
        or (isinstance(merged_at_val, str) and bool(merged_at_val.strip()))
        if isinstance(pr_data, dict)
        else False
    )
    if pr_state == "closed" or is_merged or is_registry_closed:
        logger.info(
            "🔒 PR is closed or merged (state=%s, merged=%s, registry=%s); short-circuiting execution",
            pr_state,
            is_merged,
            is_registry_closed,
        )
        return [
            ActionResult(
                tool="skip_closed_pr",
                success=True,
                detail=f"PR is closed/merged (state={pr_state}, merged={is_merged}, registry={is_registry_closed}); agent execution skipped.",
            )
        ]

    # Check mutation policy
    allow_auto = os.environ.get("ALLOW_AUTOMATED_MUTATIONS", DEFAULT_ALLOW_AUTOMATED_MUTATIONS) in (
        "1",
        "true",
        "True",
    )
    if not allow_auto and not dry_run:
        logger.debug(
            "⛔ Mutations disabled (ALLOW_AUTOMATED_MUTATIONS=%s)",
            os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "1"),
        )
        logger.info(
            "mutations disabled by policy (trace: %s)",
            trace_id[-4:] if len(trace_id) >= 4 else trace_id,
        )
        return [
            ActionResult(
                tool="plan",
                success=False,
                detail="mutations are disabled by policy",
            )
        ]

    if dry_run:
        logger.debug("🧪 Dry-run mode enabled")
        logger.info(
            "dry-run mode (trace: %s)",
            trace_id[-4:] if len(trace_id) >= 4 else trace_id,
        )
        return [
            ActionResult(
                tool="plan",
                success=True,
                detail="dry-run: would process event through ADK agent",
            )
        ]

    return None
