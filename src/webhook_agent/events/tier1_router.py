"""Tier 1 deterministic programmatic routing, debouncing, and CI gating.

Executes deterministic checks and fast-paths (0 LLM calls):
- CI suite completion handling and CI gate evaluation.
- Base branch merge synchronization detection.
- Closed / merged PR short-circuiting.
- Fast-push debouncing and duplicate review prevention.
- In-flight audit deduplication claiming.
- Automated dependency update fast-paths.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Callable
from enum import Enum
from typing import Any
from unittest.mock import NonCallableMock

from webhook_agent.commands import has_review_slash_command
from webhook_agent.constants import DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS
from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.review.ci_gate import (
    GATED_EVENTS,
    PASSING_CONCLUSIONS,
    CIState,
    build_review_event,
    evaluate_ci,
    is_ci_gate_enabled,
    last_bot_review_sha,
    pr_numbers_for_suite,
)
from webhook_agent.review.fast_path import (
    evaluate_dependency_fast_path,
    is_base_branch_merge_sync,
)
from webhook_agent.review.writeback_policy import is_automatic_pr_review_enabled

logger = logging.getLogger("webhook_agent.events.router")


class Tier1Status(Enum):
    """Result status of Tier 1 deterministic programmatic handling."""

    TERMINAL = "terminal"  # Handled programmatically or suppressed (0 LLM calls needed)
    ADVANCE = "advance"  # Preconditions passed; advance to Tier 2 LLM delegation


def _resolve_helper(name: str, default_fn: Any) -> Any:
    """Resolve helper function from tier1_router or webhook_agent.processor if patched by tests."""
    if isinstance(default_fn, NonCallableMock) or hasattr(default_fn, "assert_called"):
        return default_fn
    proc = sys.modules.get("webhook_agent.processor")
    if proc is not None and hasattr(proc, name):
        val = getattr(proc, name)
        if val is not default_fn:
            return val
    return default_fn


def handle_ci_completed(
    gh: Any,
    payload: dict[str, Any],
    process_event_fn: Callable[..., Any],
) -> None:
    """Start reviews that were held back while CI was running.

    A failed suite is ignored (silent skip). A passing one re-checks the whole commit, since
    other suites may still be running or may have failed.
    """
    raw = payload.get("raw_payload") or {}
    suite = raw.get("check_suite") or {}
    repo_name = (raw.get("repository") or {}).get("full_name") or ""
    head_sha = suite.get("head_sha") or ""
    conclusion = str(suite.get("conclusion") or "").lower()
    if not repo_name or not head_sha:
        return

    check_auto_enabled = _resolve_helper(
        "is_automatic_pr_review_enabled", is_automatic_pr_review_enabled
    )
    if not check_auto_enabled():
        logger.info(
            "CI gate: automatic reviews disabled (ENABLE_AUTOMATIC_PR_REVIEWS=%s); ignoring check_suite for %s",
            os.environ.get("ENABLE_AUTOMATIC_PR_REVIEWS", DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS),
            repo_name,
        )
        return
    if conclusion not in PASSING_CONCLUSIONS:
        logger.info(
            "CI gate: suite for %s@%s concluded %r; not reviewing.",
            repo_name,
            head_sha[:7],
            conclusion or "unknown",
        )
        return

    get_pr_numbers = _resolve_helper("pr_numbers_for_suite", pr_numbers_for_suite)
    for number in get_pr_numbers(gh, repo_name, suite):
        try:
            review_if_ci_green(gh, repo_name, number, head_sha, process_event_fn)
        except Exception:
            logger.exception("CI gate: failed to resume review for %s#%s", repo_name, number)


def review_if_ci_green(
    gh: Any,
    repo_name: str,
    number: int,
    head_sha: str,
    process_event_fn: Callable[..., Any],
) -> None:
    """Review PR ``number`` now if CI is fully green and the bot has not reviewed this commit."""
    pr = gh.get_repo(repo_name).get_pull(number)
    if pr.state != "open" or pr.head.sha != head_sha:
        return  # closed, or a newer push superseded this commit (its own CI will report)

    eval_ci_fn = _resolve_helper("evaluate_ci", evaluate_ci)
    if eval_ci_fn(gh, repo_name, head_sha) is not CIState.PASS:
        return

    last_review_fn = _resolve_helper("last_bot_review_sha", last_bot_review_sha)
    reviewed_head, last_reviewed_sha = last_review_fn(pr, head_sha)
    if reviewed_head:
        return

    logger.info(
        "CI gate: CI green for %s#%s (%s); starting review.", repo_name, number, head_sha[:7]
    )
    build_event_fn = _resolve_helper("build_review_event", build_review_event)
    event = build_event_fn(pr, repo_name, head_sha, last_reviewed_sha, f"ci-gate-{uuid.uuid4()}")
    process_event_fn(event, ci_verified=True)


def handle_tier_1_programmatic(
    gh: Any,
    payload: dict[str, Any],
    event_key: str,
    delivery_id: str,
    *,
    dry_run: bool = False,
    ci_verified: bool = False,
    handle_ci_completed_fn: Callable[[dict[str, Any]], None] | None = None,
) -> Tier1Status:
    """Execute Tier 1 deterministic operations (0 LLM calls).

    Returns:
        Tier1Status.ADVANCE if preconditions are satisfied for LLM evaluation.
        Tier1Status.TERMINAL if handled or suppressed programmatically.
    """
    if event_key == "check_suite.completed":
        if handle_ci_completed_fn:
            handle_ci_completed_fn(payload)
        return Tier1Status.TERMINAL

    raw_repo = payload.get("repository")
    if not isinstance(raw_repo, dict) and isinstance(payload.get("raw_payload"), dict):
        raw_repo = payload["raw_payload"].get("repository")
    raw_repo_name = raw_repo.get("full_name") if isinstance(raw_repo, dict) else None
    if not raw_repo_name or not isinstance(raw_repo_name, str):
        logger.warning("No repository found in event payload")
        return Tier1Status.TERMINAL
    repo_name: str = raw_repo_name

    check_merge_sync = _resolve_helper("is_base_branch_merge_sync", is_base_branch_merge_sync)
    if event_key == "pull_request.synchronize" and check_merge_sync(gh, repo_name, payload):
        raw = payload.get("raw_payload") or {}
        pr_data = raw.get("pull_request") or {}
        pr_number = pr_data.get("number", "unknown")
        base_ref = (pr_data.get("base") or {}).get("ref", "base")
        head_sha = (pr_data.get("head") or {}).get("sha", "")[:7]
        logger.info(
            "Suppressed pull_request.synchronize for PR #%s: commit %s is a base branch update (merged '%s'). Skipping execution.",
            pr_number,
            head_sha,
            base_ref,
        )
        return Tier1Status.TERMINAL

    # Short-circuit if target PR is closed or merged
    raw = payload.get("raw_payload") or {}
    pr_data = raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
    issue_data = raw.get("issue") or {}
    canonical = payload.get("canonical", "")
    is_pr_event = bool(pr_data) or canonical.startswith(
        (
            "pull_request.",
            "pull_request_review",
            "pull_request_review_comment.",
        )
    )
    pr_number = (raw.get("pull_request") or {}).get("number") or issue_data.get("number")

    pr_num_int: int | None = None
    if is_pr_event and pr_number is not None:
        try:
            pr_num_int = int(pr_number)
        except (TypeError, ValueError):
            pr_num_int = None

        raw_state = ""
        if isinstance(pr_data, dict) and pr_data.get("state"):
            raw_state = pr_data["state"]
        elif isinstance(issue_data, dict) and issue_data.get("state"):
            raw_state = issue_data["state"]

        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        merged_at_val = pr_data.get("merged_at") if isinstance(pr_data, dict) else None
        is_merged = (
            pr_data.get("merged") is True
            or (isinstance(merged_at_val, str) and bool(merged_at_val.strip()))
            if isinstance(pr_data, dict)
            else False
        )

        comment_body = (raw.get("comment") or {}).get("body", "")
        has_slash_override = bool(comment_body and has_review_slash_command(comment_body))

        # If slash command override is present or PR is verified open, ensure registry is un-closed
        if has_slash_override and pr_num_int is not None:
            if pr_closed_registry.is_closed(repo_name, pr_num_int) and gh is not None:
                try:
                    live_pr = gh.get_repo(repo_name).get_pull(pr_num_int)
                    if getattr(live_pr, "state", "").lower() == "open" and not getattr(
                        live_pr, "merged", False
                    ):
                        pr_closed_registry.unmark_closed(repo_name, pr_num_int)
                        pr_state = "open"
                        is_merged = False
                        logger.info(
                            "🔓 Slash command override verified PR %s#%s is OPEN on GitHub; cleared closed registry.",
                            repo_name,
                            pr_number,
                        )
                except Exception as ex:
                    logger.debug("Failed live PR check for slash command override: %s", ex)

        if pr_state == "open" and not is_merged:
            if pr_num_int is not None:
                pr_closed_registry.unmark_closed(repo_name, pr_num_int)
        elif (
            pr_state == "closed"
            or is_merged
            or (pr_num_int is not None and pr_closed_registry.is_closed(repo_name, pr_num_int))
        ):
            logger.info(
                "🔒 PR %s#%s is closed or merged; skipping agent execution.",
                repo_name,
                pr_number,
            )
            return Tier1Status.TERMINAL

    head_sha = (pr_data.get("head") or {}).get("sha", "") if isinstance(pr_data, dict) else ""

    # Automatic PR reviews gate: skip automatic reviews on PR lifecycle events unless enabled.
    check_auto_review = _resolve_helper(
        "is_automatic_pr_review_enabled", is_automatic_pr_review_enabled
    )
    if (
        canonical in GATED_EVENTS or canonical == "pull_request_review_requested"
    ) and not check_auto_review():
        logger.info(
            "Automatic PR review disabled (ENABLE_AUTOMATIC_PR_REVIEWS=%s); skipping %s for %s#%s",
            os.environ.get("ENABLE_AUTOMATIC_PR_REVIEWS", DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS),
            canonical,
            repo_name,
            pr_number,
        )
        return Tier1Status.TERMINAL

    # CI gate: automatic reviews wait for fully green CI. Silent on pending or failed CI.
    # This runs before the in-flight dedup claim below so that a deferred event does not
    # consume the claim and block the review once CI turns green.
    check_ci_enabled = _resolve_helper("is_ci_gate_enabled", is_ci_gate_enabled)
    eval_ci_fn = _resolve_helper("evaluate_ci", evaluate_ci)
    if not ci_verified and canonical in GATED_EVENTS and head_sha and pr_num_int is not None:
        if check_ci_enabled():
            ci_state = eval_ci_fn(gh, repo_name, head_sha)
            if ci_state is not CIState.PASS:
                logger.info(
                    "CI gate: PR %s#%s (commit %s) CI is %s; not reviewing now.",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                    ci_state.value,
                )
                return Tier1Status.TERMINAL

    # Fast-push debouncing & duplicate review gate: abort if superseded or already reviewed
    if (
        is_pr_event
        and head_sha
        and pr_num_int is not None
        and (canonical in GATED_EVENTS or canonical == "pull_request_review_requested")
        and gh is not None
    ):
        try:
            live_pr = gh.get_repo(repo_name).get_pull(pr_num_int)
            live_state = getattr(live_pr, "state", None)
            if isinstance(live_state, str) and live_state != "open":
                logger.debug(
                    "🔒 PR %s#%s is not open (state=%s); skipping.",
                    repo_name,
                    pr_number,
                    live_state,
                )
                return Tier1Status.TERMINAL
            live_head_sha = getattr(getattr(live_pr, "head", None), "sha", None)
            if isinstance(live_head_sha, str) and live_head_sha and live_head_sha != head_sha:
                logger.info(
                    "⚡ PR %s#%s commit %s superseded by live head %s; skipping evaluation.",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                    live_head_sha[:7],
                )
                return Tier1Status.TERMINAL
            last_review_fn = _resolve_helper("last_bot_review_sha", last_bot_review_sha)
            already_reviewed, _ = last_review_fn(live_pr, head_sha)
            if already_reviewed:
                logger.info(
                    "Skipping duplicate bot review for %s#%s at head %s (already reviewed)",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                )
                return Tier1Status.TERMINAL
        except Exception as live_err:
            logger.warning(
                "⚠️ Could not verify live PR head for %s#%s (commit %s): %s — "
                "deferring to avoid reviewing stale data.",
                repo_name,
                pr_number,
                head_sha[:7] if head_sha else "unknown",
                live_err,
            )
            return Tier1Status.TERMINAL

    # In-Flight Audit Deduplication: prevent concurrent webhook processing for the same PR commit
    if is_pr_event and pr_number is not None and head_sha:
        if (
            canonical
            in (
                "pull_request.opened",
                "pull_request.synchronize",
                "pull_request.ready_for_review",
                "pull_request.reopened",
            )
            and head_sha
        ):
            from webhook_agent.state.reconciliation import (
                build_reconciliation_cache_key,
                try_claim_reconciliation,
            )

            claim_fn = _resolve_helper("try_claim_reconciliation", try_claim_reconciliation)
            audit_cache_key = build_reconciliation_cache_key(repo_name, pr_number, head_sha)
            is_proactive_or_resume = str(delivery_id).startswith(
                ("proactive-reconcile-", "resume-")
            )
            if not is_proactive_or_resume and not claim_fn(audit_cache_key):
                logger.info(
                    "🔒 In-Flight Audit Deduplication: PR %s#%s (commit %s) is already in-flight or reviewed. Skipping duplicate webhook execution.",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                )
                return Tier1Status.TERMINAL

    # Deterministic Fast-Path for automated Dependabot / lockfile PRs (0 LLM calls)
    if not dry_run and is_pr_event and pr_number is not None:
        eval_dep_fast_path = _resolve_helper(
            "evaluate_dependency_fast_path", evaluate_dependency_fast_path
        )
        if eval_dep_fast_path(
            gh=gh,
            repo_name=repo_name,
            payload=payload,
            raw=raw,
            pr_data=pr_data,
            pr_number=pr_number,
            dry_run=dry_run,
        ):
            return Tier1Status.TERMINAL

    return Tier1Status.ADVANCE
