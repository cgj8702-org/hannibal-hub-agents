"""Webhook processor for the Hannibal Hub agents.

This file replaces the legacy worker logic with a more explicit, testable
router.  It is intentionally simple and heavily documented so that it can be
maintained by developers who are not familiar with the intricacies of the
GitHub webhook ecosystem.

Key responsibilities:

* Normalise GitHub webhook events into a small set of canonical categories.
* Decide whether an event should be processed based on its canonical
  value and a set of known noisy events.
* Delegate to :class:`AgentCore` for the actual agent execution.

The implementation deliberately avoids importing heavy packages until the
``process_event`` method is called.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from github import Auth, Github

from webhook_agent.constants import (
    DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS,
    DEFAULT_TRIGGER_ALLOWED_ASSOCIATIONS,
    DEFAULT_TRIGGER_EXEMPT_LOGINS,
)
from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.github.bot_identity import _is_bot_event, is_jules_sender
from webhook_agent.github.credentials import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)
from webhook_agent.github.jules_proxy import handle_jules_pr_opened
from webhook_agent.github.pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
)
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
from webhook_agent.review.formatter import (
    truncate_log_payload,
)
from webhook_agent.review.writeback_policy import is_automatic_pr_review_enabled

from .core.agent_definition import WebhookAgent
from .review.comment_poster import _add_eyes_reaction
from .state.review_checkpoint import review_checkpoint_manager
from .webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.processor")
core_logger = logging.getLogger("webhook_agent.core")

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
# prompt and a hallucinated reply (see docs/plans/repo_reclamation.md §8.1).
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
    }
)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, treating empty/unset as the default.

    ``os.getenv(name, default)`` only falls back when the variable is *unset*;
    an empty string (e.g. ``GITHUB_APP_ID=`` from a failed secret resolution)
    passes through and crashes ``int()``. This helper treats empty as unset.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default


# Re-exported module helpers (extracted to pr_context.py, fast_path.py, and review/comment_poster.py)
# Preserved for backwards compatibility with existing consumers and unit tests.
__all__ = [
    "SUPPRESSED_EVENT_NAMES",
    "SUPPRESSED_PR_METADATA_ACTIONS",
    "AgentCore",
    "WebhookProcessor",
    "_add_eyes_reaction",
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_should_prefetch_diff",
    "evaluate_dependency_fast_path",
    "generate_trace_id",
    "is_base_branch_merge_sync",
]


# ---------------------------------------------------------------------------
# LLM Agent Execution Lifecycle (Inlined from agent_core.py)
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Generate a unique hex trace ID for event execution."""
    return uuid.uuid4().hex


class AgentCore:
    """Entry point for webhook event processing and LLM agent lifecycle.

    Inlined from `agent_core.py` as part of Phase 1 of the Master Modularization
    Blueprint to flatten the indirection cascade (worker -> processor -> WebhookAgent).

    Maintains a strict separation of concerns:
    - WebhookProcessor governs HTTP delivery ingestion, dedup, and event routing.
    - AgentCore encapsulates execution lifecycle, trace propagation, and WebhookAgent invocation.
    - Future modularization (Phase 3) extracts prefetch and fast paths into separate modules
      (`pr_context.py`, `fast_path.py`) to prevent module bloat.
    """

    def __init__(self, gh_client: Any = None, dry_run: bool = False, planner: Any = None) -> None:
        self.gh = gh_client
        self.dry_run = dry_run
        self._webhook_agent = WebhookAgent(dry_run=dry_run)

    def run(
        self,
        event_data: dict[str, Any],
        repo_full_name: str,
        trace_id: str | None = None,
        gh_client: Any = None,
    ) -> list[ActionResult]:
        """Process a normalized event through the ADK-powered agent.

        Delegates all planning and execution to WebhookAgent.
        """
        trace_id = trace_id or generate_trace_id()

        core_logger.debug(
            "🧠 Starting ADK agent processing (trace: %s, repo: %s)",
            trace_id[-4:],
            repo_full_name,
        )

        core_logger.info(
            "🧠 Processing event via ADK agent (trace: %s, repo: %s)",
            trace_id[-4:],
            repo_full_name,
        )

        gh = gh_client if gh_client is not None else self.gh
        if gh is not None and "gh_client" not in event_data:
            event_data["gh_client"] = gh

        results = self._webhook_agent.plan_and_execute(
            event_data=event_data,
            gh_client=gh,
            trace_id=trace_id,
        )

        core_logger.debug(
            "🧠 ADK agent processing completed (trace: %s, result_count: %d)",
            trace_id[-4:],
            len(results) if results else 0,
        )

        return results


class WebhookProcessor:
    """Handles inbound webhook events.

    The processor expects a *normalized* payload – a dictionary that matches
    the GitHub webhook headers:

    * ``event_name`` – the X‑GitHub‑Event header value.
    * ``action`` – the action field nested inside the payload.
    * ``delivery_id`` – a unique id for the webhook delivery.
    * ``raw_payload`` – the original JSON body of the webhook.
    """

    # Maximum number of delivery IDs to retain for duplicate suppression.
    # Prevents unbounded memory growth in long-running processes.
    _MAX_PROCESSED_DELIVERIES = 10_000

    def __init__(self) -> None:
        # Keep track of processed deliveries to prevent duplicate handling (FIFO capped dict).
        self._processed_deliveries: dict[str, None] = {}
        # Load essential GitHub credentials from the environment.
        # Empty env vars (e.g. from a failed secret resolution) are treated as
        # unset so the worker fails with a clear error instead of int('') crashing.
        from webhook_agent.constants import (
            DEFAULT_GITHUB_APP_ID,
            DEFAULT_GITHUB_INSTALLATION_ID,
        )

        self.app_id = _env_int("GITHUB_APP_ID", int(DEFAULT_GITHUB_APP_ID))
        self.installation_id = _env_int(
            "GITHUB_INSTALLATION_ID", int(DEFAULT_GITHUB_INSTALLATION_ID)
        )
        self.private_key_path = os.getenv(
            "GITHUB_PRIVATE_KEY_PATH",
            "/tmp/keys/github-app-private-key.pem",  # noqa: S108 - key directory created with mode 0700 by load_secrets.sh
        )
        self.dry_run = os.environ.get("DRY_RUN", "0") in ("1", "true", "True")
        # Built lazily on first process_event() call — NOT here. Keeps
        # WebhookProcessor() cheap to construct for tests that only exercise
        # routing/filtering (see test_worker.py), and ensures the ADK
        # session/memory services inside WebhookAgent are constructed exactly
        # once and reused for the lifetime of this worker process, instead of
        # being discarded and rebuilt on every event.
        self._agent_core: AgentCore | None = None
        self._gh: Github | None = None
        self._gh_token_expires_at: float | None = None
        self._force_github_token_refresh = False

    @property
    def gh(self) -> Github:
        """Return an authenticated Github client, creating or loading cached installation token."""
        if self._gh is not None and (
            self._gh_token_expires_at is None or time.time() < self._gh_token_expires_at - 60
        ):
            return self._gh
        inst_token = (
            None if self._force_github_token_refresh else load_cached_token(self.installation_id)
        )
        if inst_token is None:
            pem = load_private_key(self.private_key_path)
            jwt_token = generate_jwt(self.app_id, pem)
            inst_token = get_installation_token(jwt_token, self.installation_id)
            save_cached_token(self.installation_id, inst_token)
            self._force_github_token_refresh = False
        self._gh = Github(auth=Auth.Token(inst_token.token))
        expires_at = getattr(inst_token, "expires_at", None)
        if expires_at:
            try:
                self._gh_token_expires_at = (
                    datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    .astimezone(UTC)
                    .timestamp()
                )
            except (AttributeError, TypeError, ValueError):
                self._gh_token_expires_at = None
        else:
            self._gh_token_expires_at = None
        return self._gh

    def invalidate_github_client(self) -> None:
        """Discard the cached client after an authentication failure."""
        self._gh = None
        self._gh_token_expires_at = None
        self._force_github_token_refresh = True

    def _get_agent_core(self) -> AgentCore:
        """Return or lazily construct the AgentCore singleton."""
        if self._agent_core is None:
            self._agent_core = AgentCore(
                gh_client=self.gh,
                dry_run=self.dry_run,
            )
        return self._agent_core

    # ---------------------------------------------------------------------------
    # Routing helpers
    # ---------------------------------------------------------------------------
    def route_event(self, ev: dict[str, Any]) -> str:
        """Translate a GitHub event into a canonical internal event string.

        Handles unknown events gracefully.
        """
        if ev.get("canonical"):
            return str(ev["canonical"])
        event_name = ev.get("event_name")
        action = ev.get("action")
        if event_name == "ping":
            return "ping"
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

    def should_process_event(self, ev: dict[str, Any]) -> bool:
        """Apply loop‑avoidance, noise filtering, and duplication checks.

        * Duplicate deliveries are suppressed.
        * Bot events originating from the app are suppressed.
        * Comments mentioning @dependabot or @coderabbitai are suppressed.
        * The ``edited`` action is filtered out.
        * check_suite.completed is let through (it re-triggers reviews held back by the CI
          gate); other automated CI noise events (check_run, status) are suppressed.
        * Read-only PR lifecycle events (pull_request.closed) are suppressed.
        * pull_request.synchronize is NOT suppressed — it is handled by the agent
          for incremental reviews of newly pushed commits.
        """
        delivery_id = ev.get("delivery_id")
        if delivery_id in self._processed_deliveries:
            return False
        if ev.get("event_name") == "check_suite" and ev.get("action") == "completed":
            # Finished CI drives deferred reviews (see review/ci_gate.py). Its sender is
            # usually github-actions[bot], which the bot-sender filter below would drop.
            return is_ci_gate_enabled()
        if _is_bot_event(ev):
            return False

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
                    return False

        comment = raw.get("comment") or raw.get("review_comment") or {}
        comment_body = comment.get("body") or ""
        cb_lower = comment_body.lower()
        if "@dependabot" in cb_lower:
            return False
        if any(bot in cb_lower for bot in ("@coderabbitai", "@coderabbitai[bot]")):
            logger.debug(
                "Suppressing comment mentioning CodeRabbit (delivery_id=%s)",
                delivery_id,
            )
            return False

        # Prevent bot-to-bot conversational ping-pong loops:
        # If a comment is authored by Jules (via login, substring, or performed_via_github_app),
        # suppress it UNLESS it explicitly requests a review or explicitly mentions @hannibal-hub-agents.
        event_name = ev.get("event_name")
        if event_name in ("issue_comment", "pull_request_review_comment"):
            comment_user = comment.get("user") or {}
            sender = ev.get("sender") or {}
            if is_jules_sender(comment_user, raw) or is_jules_sender(sender, raw):
                from .review.writeback_policy import REVIEW_INTENT_KEYWORDS

                cb_lower = comment_body.lower()
                has_review_intent = any(cmd in cb_lower for cmd in REVIEW_INTENT_KEYWORDS)
                is_explicitly_mentioned = "@hannibal-hub-agents" in cb_lower
                if not (has_review_intent or is_explicitly_mentioned):
                    logger.debug(
                        "Suppressing conversational reply to Jules comment (no review intent / mention)"
                    )
                    return False

        action = ev.get("action")
        if action == "deleted":
            event_name = ev.get("event_name")
            if event_name in (
                "issue_comment",
                "pull_request_review_comment",
                "pull_request",
                "issue",
            ):
                return False

        if action == "edited":
            sender_login = (ev.get("sender") or {}).get("login", "")
            raw = ev.get("raw_payload") or {}
            comment_user = (raw.get("comment") or {}).get("user") or {}
            comment_author = comment_user.get("login", "")
            allowed_users = {"cgj8702", "cgj8702-agents"}
            if sender_login not in allowed_users and comment_author not in allowed_users:
                return False

        event_name = ev.get("event_name")
        canonical = self.route_event(ev)
        # Suppress review-submission events to prevent self-review feedback loops. A submitted
        # review says nothing about whether the PR is closed, so this arm must NOT register the
        # PR in the closed registry — doing so poisons every later event for the registry's TTL
        # window and silently skips the agent on an open PR (observed on #267).
        if canonical == "pull_request_review.submitted":
            return False

        # Suppress genuinely closed PR events and register them so in-flight agent work short-circuits.
        # Accept both canonical spellings and the "pr" event-name alias: route_event's fallback yields
        # "pr.closed" for event_name="pr" (not "pull_request.closed"), so without the alias a pr-aliased
        # closed event would slip through unregistered. Defensive per CodeRabbit #268 — the
        # X-GitHub-Event header only ever sends "pull_request" today, but the alias keeps the branch total.
        if canonical in ("pull_request.closed", "pr.closed") or (
            event_name in ("pull_request", "pr") and action == "closed"
        ):
            raw = ev.get("raw_payload") or {}
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
            return False

        if canonical.startswith("pull_request_review_thread."):
            return False

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
            return False

        # Ignore automated CI infrastructure noise, installation lifecycle events, and review
        # thread state changes. See SUPPRESSED_EVENT_NAMES.
        return event_name not in SUPPRESSED_EVENT_NAMES

    def _handle_ci_completed(self, payload: dict[str, Any]) -> None:
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
        if not is_automatic_pr_review_enabled():
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

        gh = self.gh
        for number in pr_numbers_for_suite(gh, repo_name, suite):
            try:
                self._review_if_ci_green(gh, repo_name, number, head_sha)
            except Exception:
                logger.exception("CI gate: failed to resume review for %s#%s", repo_name, number)

    def _review_if_ci_green(self, gh: Any, repo_name: str, number: int, head_sha: str) -> None:
        """Review PR ``number`` now if CI is fully green and the bot has not reviewed this commit."""
        pr = gh.get_repo(repo_name).get_pull(number)
        if pr.state != "open" or pr.head.sha != head_sha:
            return  # closed, or a newer push superseded this commit (its own CI will report)
        if evaluate_ci(gh, repo_name, head_sha) is not CIState.PASS:
            return
        reviewed_head, last_reviewed_sha = last_bot_review_sha(pr, head_sha)
        if reviewed_head:
            return
        logger.info(
            "CI gate: CI green for %s#%s (%s); starting review.", repo_name, number, head_sha[:7]
        )
        event = build_review_event(
            pr, repo_name, head_sha, last_reviewed_sha, f"ci-gate-{uuid.uuid4()}"
        )
        self.process_event(event, ci_verified=True)

    def process_event(self, payload: dict[str, Any], *, ci_verified: bool = False) -> None:
        """Process a Pub/Sub payload.

        Logs the canonical event, filters duplication and bot events,
        and delegates execution to AgentCore.
        """
        event_key = self.route_event(payload)
        if event_key == "unknown":
            logger.debug(
                "Unknown event (event_name=%s, delivery_id=%s)",
                payload.get("event_name"),
                payload.get("delivery_id"),
            )
        logger.debug("Processing event: %s", event_key)
        if not self.should_process_event(payload):
            return

        delivery_id = payload.get("delivery_id", "unknown")
        if delivery_id != "unknown":
            self._processed_deliveries[delivery_id] = None
            # Bound the dict size to avoid unbounded memory growth (FIFO eviction).
            if len(self._processed_deliveries) > self._MAX_PROCESSED_DELIVERIES:
                oldest_delivery_id = next(iter(self._processed_deliveries))
                del self._processed_deliveries[oldest_delivery_id]

        logger.info("Event processed: %s", event_key)

        # Set canonical event name in payload for AgentCore and WebhookAgent
        payload["canonical"] = event_key

        try:
            self._dispatch_event(payload, event_key, delivery_id, ci_verified=ci_verified)
        except Exception:
            if delivery_id != "unknown":
                self._processed_deliveries.pop(delivery_id, None)
            raise

    def _dispatch_event(
        self,
        payload: dict[str, Any],
        event_key: str,
        delivery_id: str,
        *,
        ci_verified: bool = False,
    ) -> None:
        if event_key == "check_suite.completed":
            self._handle_ci_completed(payload)
            return

        gh = self.gh

        agent = self._get_agent_core()

        raw_repo = payload.get("repository")
        if not isinstance(raw_repo, dict) and isinstance(payload.get("raw_payload"), dict):
            raw_repo = payload["raw_payload"].get("repository")
        repo_name = raw_repo.get("full_name") if isinstance(raw_repo, dict) else None
        if not repo_name:
            logger.warning("No repository found in event payload")
            return

        logger.info("Agent starting execution for repo %s", repo_name)

        dry_run = (os.environ.get("DRY_RUN", "0") in ("1", "true", "True")) or self.dry_run
        if handle_jules_pr_opened(gh, repo_name, payload, dry_run=dry_run):
            logger.info("Jules PR adoption proxy handled PR in %s; stopping processing.", repo_name)
            return

        if event_key == "pull_request.synchronize" and is_base_branch_merge_sync(
            gh, repo_name, payload
        ):
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
            return

        # Short-circuit if target PR is closed or merged
        raw = payload.get("raw_payload") or {}
        pr_data = raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
        canonical = payload.get("canonical", "")
        is_pr_event = bool(pr_data) or canonical.startswith(
            (
                "pull_request.",
                "pull_request_review",
                "pull_request_review_comment.",
            )
        )
        pr_number = (
            pr_data.get("number")
            if isinstance(pr_data, dict)
            else (raw.get("issue") or {}).get("number")
        )

        pr_num_int: int | None = None
        if is_pr_event and pr_number is not None:
            try:
                pr_num_int = int(pr_number)
            except (TypeError, ValueError):
                pr_num_int = None

            raw_state = pr_data.get("state") if isinstance(pr_data, dict) else ""
            pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
            merged_at_val = pr_data.get("merged_at") if isinstance(pr_data, dict) else None
            is_merged = (
                pr_data.get("merged") is True
                or (isinstance(merged_at_val, str) and bool(merged_at_val.strip()))
                if isinstance(pr_data, dict)
                else False
            )
            if (
                pr_state == "closed"
                or is_merged
                or (pr_num_int is not None and pr_closed_registry.is_closed(repo_name, pr_num_int))
            ):
                logger.info(
                    "🔒 PR %s#%s is closed or merged; skipping agent execution.",
                    repo_name,
                    pr_number,
                )
                return

        head_sha = (pr_data.get("head") or {}).get("sha", "") if isinstance(pr_data, dict) else ""

        # Automatic PR reviews gate: skip automatic reviews on PR lifecycle events unless enabled.
        if (
            canonical in GATED_EVENTS or canonical == "pull_request_review_requested"
        ) and not is_automatic_pr_review_enabled():
            logger.info(
                "Automatic PR review disabled (ENABLE_AUTOMATIC_PR_REVIEWS=%s); skipping %s for %s#%s",
                os.environ.get("ENABLE_AUTOMATIC_PR_REVIEWS", DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS),
                canonical,
                repo_name,
                pr_number,
            )
            return

        # CI gate: automatic reviews wait for fully green CI. Silent on pending or failed CI.
        # This runs before the in-flight dedup claim below so that a deferred event does not
        # consume the claim and block the review once CI turns green.
        if (
            not ci_verified
            and canonical in GATED_EVENTS
            and head_sha
            and pr_num_int is not None
            and is_ci_gate_enabled()
        ):
            ci_state = evaluate_ci(gh, repo_name, head_sha)
            if ci_state is not CIState.PASS:
                logger.info(
                    "CI gate: PR %s#%s (commit %s) CI is %s; not reviewing now.",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                    ci_state.value,
                )
                return

        # Fast-push debouncing: abort if head commit has been superseded by a newer push
        if (
            is_pr_event
            and head_sha
            and pr_num_int is not None
            and canonical in GATED_EVENTS
            and gh is not None
        ):
            try:
                live_pr = gh.get_repo(repo_name).get_pull(pr_num_int)
                live_state = getattr(live_pr, "state", None)
                if isinstance(live_state, str) and live_state != "open":
                    logger.info(
                        "🔒 PR %s#%s is not open (state=%s); skipping.",
                        repo_name,
                        pr_number,
                        live_state,
                    )
                    return
                live_head_sha = getattr(getattr(live_pr, "head", None), "sha", None)
                if isinstance(live_head_sha, str) and live_head_sha and live_head_sha != head_sha:
                    logger.info(
                        "⚡ PR %s#%s commit %s superseded by live head %s; skipping evaluation.",
                        repo_name,
                        pr_number,
                        head_sha[:7],
                        live_head_sha[:7],
                    )
                    return
            except Exception as live_err:
                logger.warning(
                    "⚠️ Could not verify live PR head for %s#%s (commit %s): %s — "
                    "deferring to avoid reviewing stale data.",
                    repo_name,
                    pr_number,
                    head_sha[:7] if head_sha else "unknown",
                    live_err,
                )
                return

        resumed_checkpoint: dict[str, Any] | None = None
        if is_pr_event and pr_num_int is not None and head_sha:
            resumed_checkpoint = review_checkpoint_manager.get_resumable(
                repo_name, pr_num_int, head_sha
            )
            if resumed_checkpoint:
                logger.info(
                    "🔁 PR %s#%s (commit %s) has resumable checkpoint; skipping diff and history prefetch.",
                    repo_name,
                    pr_number,
                    head_sha[:7],
                )
                payload["resumed_checkpoint"] = resumed_checkpoint
                if not raw.get("pr_diff") and resumed_checkpoint.get("pr_diff"):
                    raw["pr_diff"] = resumed_checkpoint["pr_diff"]
                if not raw.get("diff_sha") and resumed_checkpoint.get("diff_sha"):
                    raw["diff_sha"] = resumed_checkpoint["diff_sha"]
                if not payload.get("precompiled_dossier") and resumed_checkpoint.get(
                    "precompiled_dossier"
                ):
                    payload["precompiled_dossier"] = resumed_checkpoint["precompiled_dossier"]
                if not payload.get("changed_files") and resumed_checkpoint.get("changed_files"):
                    payload["changed_files"] = resumed_checkpoint["changed_files"]

        dry_run = os.environ.get("DRY_RUN", "0") in ("1", "true", "True")
        if not dry_run and not resumed_checkpoint:
            _prefetch_pr_diff(gh, repo_name, payload)
            _prefetch_inline_comment_context(gh, repo_name, payload)
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

            # Re-check closed/merged state if live prefetch discovered it was closed on GitHub
            if is_pr_event and pr_num_int is not None:
                updated_pr = (
                    raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
                )
                updated_state = (
                    (updated_pr.get("state") or "").lower() if isinstance(updated_pr, dict) else ""
                )
                if updated_state == "closed" or pr_closed_registry.is_closed(repo_name, pr_num_int):
                    logger.info(
                        "🔒 PR %s#%s was closed or merged on GitHub; skipping agent execution.",
                        repo_name,
                        pr_number,
                    )
                    return

        # In-Flight Audit Deduplication: prevent concurrent runs across webhooks & proactive sweeps
        audit_cache_key: str | None = None
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
                from webhook_agent.review.proactive_service import (
                    build_reconciliation_cache_key,
                    try_claim_reconciliation,
                )

                audit_cache_key = build_reconciliation_cache_key(repo_name, pr_number, head_sha)
                is_proactive_event = str(delivery_id).startswith("proactive-reconcile-")
                if not is_proactive_event and not try_claim_reconciliation(audit_cache_key):
                    logger.info(
                        "🔒 In-Flight Audit Deduplication: PR %s#%s (commit %s) is already in-flight or reviewed. Skipping duplicate webhook execution.",
                        repo_name,
                        pr_number,
                        head_sha[:7],
                    )
                    return

        # Guarded Eyes Reaction: only react once closed/merged PR and deduplication guardrails pass
        if not dry_run:
            _add_eyes_reaction(gh, repo_name, payload)

        # Deterministic Fast-Path for automated Dependabot / lockfile PRs
        if not dry_run and is_pr_event and pr_number is not None:
            if evaluate_dependency_fast_path(
                gh=gh,
                repo_name=repo_name,
                payload=payload,
                raw=raw,
                pr_data=pr_data,
                pr_number=pr_number,
                dry_run=dry_run,
            ):
                return

        try:
            results = agent.run(payload, repo_name, gh_client=gh)
        except Exception:
            if audit_cache_key:
                from webhook_agent.review.proactive_service import (
                    release_reconciliation_claim,
                )

                release_reconciliation_claim(audit_cache_key)
            raise
        if results:
            for r in results:
                msg = getattr(r, "detail", None) or getattr(r, "message", str(r))
                status_symbol = "OK" if r.success else "FAIL"
                tool_name = getattr(r, "tool", "")
                tool_label = f" (tool: {tool_name})" if tool_name else ""
                logger.info(
                    "Agent action [%s]%s: %s",
                    status_symbol,
                    tool_label,
                    truncate_log_payload(msg, 300),
                )
        else:
            logger.info(
                "Agent completed execution with no actions taken for repo %s",
                repo_name,
            )
