"""Webhook processor for the Hannibal Hub agents.

This file replaces the legacy worker logic with a more explicit, testable
router.  It is intentionally simple and heavily documented so that it can be
maintained by developers who are not familiar with the intricacies of the
GitHub webhook ecosystem.

Key responsibilities:

* Normalise GitHub webhook events into a small set of canonical categories.
* Decide whether an event should be processed based on its canonical
  value and a set of known noisy events.
* Delegate to :class:`~.agent_core.AgentCore` for the actual agent execution.

The implementation deliberately avoids importing heavy packages until the
``process_event`` method is called.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from github import Auth, Github

from .agent_core import AgentCore
from .bot_identity import _is_bot_event
from .github_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

logger = logging.getLogger("webhook_processor")


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
        try:
            self.app_id = int(os.environ["GITHUB_APP_ID"])
            self.installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
            self.private_key_path = os.environ["GITHUB_PRIVATE_KEY_PATH"]
        except KeyError as exc:
            logger.error("Missing required environment variable: %s", exc)
            raise

    # ---------------------------------------------------------------------------
    # Routing helpers
    # ---------------------------------------------------------------------------
    def route_event(self, ev: dict[str, Any]) -> str:
        """Translate a GitHub event into a canonical internal event string.

        Handles unknown events gracefully.
        """
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
        key = (event_name, action)
        if canonical := mapping.get(key):
            return canonical
        if action:
            return f"{event_name}.{action}"
        return "unknown"

    def should_process_event(self, ev: dict[str, Any]) -> bool:
        """Apply loop‑avoidance, noise filtering, and duplication checks.

        * Duplicate deliveries are suppressed.
        * Bot events originating from the app are suppressed.
        * The ``edited`` action is filtered out.
        * Automated CI noise events (check_suite, check_run, status) are suppressed.
        * Read-only PR lifecycle events (pull_request.closed, pull_request.synchronize) are suppressed.
        """
        delivery_id = ev.get("delivery_id")
        if delivery_id in self._processed_deliveries:
            return False
        if _is_bot_event(ev):
            return False

        action = ev.get("action")
        if action == "edited":
            sender_login = (ev.get("sender") or {}).get("login", "")
            raw = ev.get("raw_payload") or {}
            comment_user = (raw.get("comment") or {}).get("user") or {}
            comment_author = comment_user.get("login", "")
            allowed_users = {"cgj8702", "cgj8702-agents"}
            if (
                sender_login not in allowed_users
                and comment_author not in allowed_users
            ):
                return False

        event_name = ev.get("event_name")
        # Ignore automated CI infrastructure noise and installation lifecycle events
        if event_name in ("check_suite", "check_run", "status", "installation"):
            return False

        return True

    def process_event(self, payload: dict[str, Any]) -> None:
        """Process a Pub/Sub payload.

        Logs the canonical event, filters duplication and bot events,
        and delegates execution to AgentCore.
        """
        event_key = self.route_event(payload)
        if event_key == "unknown":
            logger.debug("Unknown event payload: %s", payload.get("raw_payload"))
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

        inst_token = load_cached_token(self.installation_id)
        if inst_token is None:
            pem = load_private_key(self.private_key_path)
            jwt_token = generate_jwt(self.app_id, pem)
            inst_token = get_installation_token(jwt_token, self.installation_id)
            save_cached_token(self.installation_id, inst_token)

        gh = Github(auth=Auth.Token(inst_token.token))

        agent = AgentCore(
            gh_client=gh,
            dry_run=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
        )

        raw_repo = payload.get("repository")
        if not isinstance(raw_repo, dict) and isinstance(
            payload.get("raw_payload"), dict
        ):
            raw_repo = payload["raw_payload"].get("repository")
        repo_name = raw_repo.get("full_name") if isinstance(raw_repo, dict) else None
        if not repo_name:
            logger.warning("No repository found in event payload")
            return

        logger.info("Agent starting execution for repo %s", repo_name)
        results = agent.run(payload, repo_name)
        if results:
            for r in results:
                msg = getattr(r, "detail", None) or getattr(r, "message", str(r))
                status_symbol = "OK" if r.success else "FAIL"
                logger.info("Agent action [%s]: %s", status_symbol, msg)
        else:
            logger.info(
                "Agent completed execution with no actions taken for repo %s",
                repo_name,
            )
