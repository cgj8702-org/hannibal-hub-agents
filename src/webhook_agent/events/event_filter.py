"""Tier 0 perimeter event filtering and canonical event routing.

Provides fast, deterministic perimeter drops (0 compute, 0 API calls) for noisy,
unauthorized, bot-generated, or suppressed webhook events.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from webhook_agent.constants import (
    DEFAULT_TRIGGER_ALLOWED_ASSOCIATIONS,
    DEFAULT_TRIGGER_EXEMPT_LOGINS,
)
from webhook_agent.github.bot_identity import _is_bot_event
from webhook_agent.review.ci_gate import is_ci_gate_enabled

logger = logging.getLogger("webhook_agent.events.filter")

SUPPRESSED_PR_METADATA_ACTIONS: frozenset[str] = frozenset(
    {
        "assigned",
        "auto_merge_disabled",
        "auto_merge_enabled",
        "converted_to_draft",
        "demilestoned",
        "dequeued",
        "enqueued",
        "labeled",
        "locked",
        "milestoned",
        "review_request_removed",
        "unassigned",
        "unlabeled",
        "unlocked",
    }
)

# Event names dropped before dispatch. Named (rather than inlined at the end of
# should_process_event) so the router's perimeter filter can be asserted as a strict subset
# of it -- see tests/unit/router/test_router.py. A router that drops more than this list
# would lose events silently, so the subset relation is the invariant that matters.
#
# CI infrastructure events: We subscribe to check_suite only -- its `completed` action
# drives the CI gate (see review/ci_gate.py). check_run / status / workflow_run / workflow_job
# are NOT subscribed, and are listed here as guards so they can never reach the agent if a
# subscription is ever re-added. No code path reads their payloads, and
# build_transient_turn_content has no branch for them -- so reaching the agent means an empty
# prompt and a hallucinated reply.
SUPPRESSED_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "check_suite",
        "check_run",
        "status",
        "installation",
        "installation_repositories",
        "installation_target",
        "pull_request_review_thread",
        "workflow_run",
        "workflow_job",
        "star",
        "watch",
        "fork",
        "release",
        "sponsorship",
        "milestone",
        "label",
    }
)


def route_event(ev: dict[str, Any]) -> str:
    """Translate a GitHub event into a canonical internal event string.

    Handles unknown events gracefully.
    """
    if ev.get("canonical"):
        return str(ev["canonical"])
    event_name = ev.get("event_name")
    action = ev.get("action")
    if event_name == "ping":
        return "ping"
    if event_name == "push":
        return "push"
    mapping = {
        ("pull_request", "opened"): "pull_request.opened",
        ("pull_request", "synchronize"): "pull_request.synchronize",
        ("pull_request", "closed"): "pull_request.closed",
        ("pull_request", "ready_for_review"): "pull_request.ready_for_review",
        ("pull_request", "reopened"): "pull_request.reopened",
        ("issue_comment", "created"): "issue_comment.created",
        (
            "pull_request_review_comment",
            "created",
        ): "pull_request_review_comment.created",
        ("pull_request_review", "submitted"): "pull_request_review.submitted",
        ("pull_request", "review_requested"): "pull_request_review_requested",
        ("label", "created"): "label.created",
        ("label", "deleted"): "label.deleted",
        ("installation", "created"): "installation.created",
        ("installation", "deleted"): "installation.deleted",
    }
    key = (str(event_name or ""), str(action or ""))
    if canonical := mapping.get(key):
        return canonical
    if action:
        return f"{event_name}.{action}"
    return "unknown"


def is_actionable_issue_opened(ev: dict[str, Any]) -> bool:
    """Return True if an issues.opened event explicitly requests agent action.

    Treats untargeted issue openings as informative record-only events (Tier 0 drop),
    preventing unprompted multi-turn tool loops on general bug reports or discussions.
    """
    if os.environ.get("AUTO_TRIAGE_NEW_ISSUES", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True

    raw = ev.get("raw_payload") or {}
    sender = ev.get("sender") or raw.get("sender") or {}
    sender_login = (sender.get("login") or "").strip().lower()

    # Partner bots (e.g. Cline) that file issues to be triaged per #441
    from webhook_agent.core.policy import PARTNER_BOT_LOGINS

    if sender_login in PARTNER_BOT_LOGINS:
        return True

    issue = raw.get("issue") or {}
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    combined = f"{title}\n{body}"

    # Explicit mention of bot
    if "@hannibal-hub-agents" in combined or "hannibal-hub-agents" in combined:
        return True

    # Slash commands: review or triage directives
    from webhook_agent.core.commands import has_review_slash_command

    if has_review_slash_command(combined):
        return True

    return any(
        bool(re.search(rf"(?<!\S){re.escape(cmd)}\b", combined)) for cmd in ("/triage", "/help")
    )


def filter_tier_0(
    ev: dict[str, Any],
    processed_deliveries: Collection[str] | Mapping[str, Any] | None = None,
    route_event_fn: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[bool, str]:
    """Apply Tier 0 pure perimeter drops (0 compute, 0 API calls).

    Returns:
        tuple[bool, str]: (should_drop, reason). True if event should be dropped.
    """
    delivery_id = ev.get("delivery_id")
    if processed_deliveries is not None and delivery_id in processed_deliveries:
        return True, "duplicate delivery"
    if ev.get("event_name") == "check_suite" and ev.get("action") == "completed":
        # Finished CI drives deferred reviews (see review/ci_gate.py).
        if not is_ci_gate_enabled():
            return True, "ci gate disabled"
        return False, "check_suite.completed (actionable)"
    if ev.get("event_name") == "push" or ev.get("canonical") == "push":
        return True, "push event"
    if _is_bot_event(ev):
        return True, "bot event"

    raw = ev.get("raw_payload") or {}
    event_name = ev.get("event_name")

    # Gated trigger authorization (T-03): restrict agent execution based on author association.
    association: str | None = None
    author_login = ""

    if event_name in ("issue_comment", "pull_request_review_comment"):
        comment = raw.get("comment") or raw.get("review_comment") or {}
        association = comment.get("author_association")
        author_login = (comment.get("user") or {}).get("login", "")
    elif event_name in ("pull_request", "pr"):
        pr_data = raw.get("pull_request") or {}
        association = pr_data.get("author_association")
        author_login = (pr_data.get("user") or {}).get("login", "")
    else:
        comment = raw.get("comment") or raw.get("review_comment") or {}

    if association and isinstance(association, str):
        allowed_raw = os.getenv(
            "TRIGGER_ALLOWED_ASSOCIATIONS", DEFAULT_TRIGGER_ALLOWED_ASSOCIATIONS
        )
        allowed_associations = {a.strip().upper() for a in allowed_raw.split(",") if a.strip()}

        if association.strip().upper() not in allowed_associations:
            exempt_raw = os.getenv("TRIGGER_EXEMPT_LOGINS", DEFAULT_TRIGGER_EXEMPT_LOGINS)
            exempt_logins = {
                login.strip().lower() for login in exempt_raw.split(",") if login.strip()
            }

            sender_login = (ev.get("sender") or raw.get("sender") or {}).get("login", "")
            if (
                sender_login.lower() not in exempt_logins
                and author_login.lower() not in exempt_logins
            ):
                logger.info(
                    "Ignoring event with unauthorized author_association=%s (delivery_id=%s)",
                    association,
                    delivery_id,
                )
                return True, f"unauthorized author_association={association}"

    comment = raw.get("comment") or raw.get("review_comment") or {}
    comment_body = comment.get("body") or ""
    cb_lower = comment_body.lower()
    if "@dependabot" in cb_lower:
        return True, "comment mentions @dependabot"
    if any(bot in cb_lower for bot in ("@coderabbitai", "@coderabbitai[bot]")):
        logger.debug(
            "Suppressing comment mentioning CodeRabbit (delivery_id=%s)",
            delivery_id,
        )
        return True, "comment mentions CodeRabbit"

    action = ev.get("action")
    if action == "deleted":
        if event_name in (
            "issue_comment",
            "pull_request_review_comment",
            "pull_request",
            "issue",
        ):
            return True, "deleted action"

    if action == "edited":
        sender_login = (ev.get("sender") or {}).get("login", "")
        raw = ev.get("raw_payload") or {}
        comment_user = (raw.get("comment") or {}).get("user") or {}
        comment_author = comment_user.get("login", "")
        allowed_users = {"cgj8702", "cgj8702-agents"}
        if sender_login not in allowed_users and comment_author not in allowed_users:
            return True, "edited action from non-allowed user"

    route_fn = route_event_fn or route_event
    canonical = route_fn(ev)
    # Suppress review-submission events to prevent self-review feedback loops. A submitted
    # review says nothing about whether the PR is closed, so this arm must NOT register the
    # PR in the closed registry — doing so poisons every later event for the registry's TTL
    # window and silently skips the agent on an open PR (observed on #267).
    if canonical == "pull_request_review.submitted":
        return True, "pull_request_review.submitted (loop avoidance)"

    # Suppress genuinely closed PR events and register them so in-flight agent work short-circuits.
    # Accept both canonical spellings and the "pr" event-name alias: route_event's fallback yields
    # "pr.closed" for event_name="pr" (not "pull_request.closed"), so without the alias a pr-aliased
    # closed event would slip through unregistered. Defensive per CodeRabbit #268 — the
    # X-GitHub-Event header only ever sends "pull_request" today, but the alias keeps the branch total.
    if canonical in ("pull_request.closed", "pr.closed") or (
        event_name in ("pull_request", "pr") and action == "closed"
    ):
        repo = raw.get("repository") or {}
        repo_full_name = repo.get("full_name", "")
        pr_data = raw.get("pull_request") or {}
        pr_number = pr_data.get("number")
        if repo_full_name and pr_number:
            try:
                from webhook_agent.core.cancellation import pr_closed_registry

                pr_closed_registry.mark_closed(repo_full_name, int(pr_number))
            except Exception as ex:
                logger.warning(
                    "Failed to mark PR %s#%s closed in registry: %s",
                    repo_full_name,
                    pr_number,
                    ex,
                )
        return True, "closed PR event"

    if canonical.startswith("pull_request_review_thread."):
        return True, "review thread event"

    # Suppress non-code metadata noise (labels, assignees, milestones, locks)
    # on both pull requests and issues to prevent unsolicited conversational comments and review noise.
    if (
        action in SUPPRESSED_PR_METADATA_ACTIONS
        and (
            event_name in ("pull_request", "pr", "issues", "issue")
            or canonical.startswith(("pull_request.", "pr.", "issues.", "issue."))
        )
    ) or (
        canonical.startswith(("pull_request.", "pr.", "issues.", "issue."))
        and canonical.split(".", 1)[1] in SUPPRESSED_PR_METADATA_ACTIONS
    ):
        logger.debug(
            "Suppressing metadata event '%s' (action=%s) to prevent noise",
            canonical,
            action,
        )
        return True, f"suppressed metadata action {action}"

    # Tier 0 Drop Guard: untargeted issues.opened events.
    # Unless the issue explicitly tags @hannibal-hub-agents, includes a review/triage directive,
    # or originates from an authorized partner bot, drop as record-only to prevent unprompted loops.
    if canonical == "issues.opened" or (event_name in ("issues", "issue") and action == "opened"):
        if not is_actionable_issue_opened(ev):
            logger.debug(
                "Suppressing untargeted issues.opened event (delivery_id=%s): no explicit mention or triage directive",
                delivery_id,
            )
            return True, "untargeted issues.opened (record only)"

    # Ignore automated CI infrastructure noise, installation lifecycle events, and review
    # thread state changes. See SUPPRESSED_EVENT_NAMES.
    if event_name in SUPPRESSED_EVENT_NAMES:
        return True, f"suppressed event name {event_name}"

    return False, "actionable"


def should_process_event(
    ev: dict[str, Any],
    processed_deliveries: Collection[str] | Mapping[str, Any] | None = None,
    route_event_fn: Callable[[dict[str, Any]], str] | None = None,
) -> bool:
    """Apply Tier 0 loop-avoidance, noise filtering, and duplication checks.

    Returns True if event passed Tier 0 and is eligible for Tier 1 programmatic handling.
    """
    should_drop, reason = filter_tier_0(
        ev, processed_deliveries=processed_deliveries, route_event_fn=route_event_fn
    )
    if should_drop:
        logger.debug(
            "Tier 0 dropped event: %s (delivery_id=%s)",
            reason,
            ev.get("delivery_id"),
        )
        return False
    return True
