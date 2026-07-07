"""Webhook Processor — handles event routing, loop protection, and agent orchestration.

This module encapsulates the logic previously found in worker.py, allowing it to be
used by either a standalone worker or integrated directly into the FastAPI app.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from github import Auth, Github

from .agent_core import AgentCore
from .github_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

logger = logging.getLogger("processor")

# Bot identity — used for loop-avoidance
BOT_LOGIN = "hannibal-hub-agents[bot]"


class WebhookProcessor:
    def __init__(self):
        self._processed_deliveries: set[str] = set()

        # Load config from env
        try:
            self.app_id = int(os.environ["GITHUB_APP_ID"])
            self.installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
            self.private_key_path = os.environ["GITHUB_PRIVATE_KEY_PATH"]
        except KeyError as e:
            logger.error(f"Missing required environment variable: {e}")
            raise

    def route_event(self, normalized: dict[str, Any]) -> str:
        """Map a normalized webhook event to a canonical internal event category."""
        event_name = normalized.get("event_name", "")
        action = normalized.get("action") or ""
        raw = normalized.get("raw_payload", {})

        if event_name == "pull_request" and action == "review_requested":
            return "pull_request_review_requested"

        if event_name == "pull_request":
            pr_action = raw.get("action", "")
            if pr_action in (
                "opened",
                "synchronize",
                "closed",
                "ready_for_review",
                "reopened",
            ):
                return f"pull_request.{pr_action}"
            return f"pull_request.{pr_action}" if pr_action else "pull_request"

        if event_name == "issue_comment":
            return f"issue_comment.{action}" if action else "issue_comment"

        if event_name == "pull_request_review_comment":
            return (
                f"pull_request_review_comment.{action}"
                if action
                else "pull_request_review_comment"
            )

        if event_name == "pull_request_review":
            return f"pull_request_review.{action}" if action else "pull_request_review"

        if event_name == "label":
            return f"label.{action}" if action else "label"

        if event_name == "installation":
            return f"installation.{action}" if action else "installation"

        if event_name == "ping":
            return "ping"

        return "unknown"

    def should_process_event(self, normalized: dict[str, Any]) -> bool:
        """Determine whether the event should be processed or suppressed."""
        delivery_id = normalized.get("delivery_id", "")
        canonical = self.route_event(normalized)

        ignored_events = {
            "pull_request.closed",
            "pull_request.synchronize",
            "check_suite.requested",
            "check_suite.completed",
            "issue_comment.deleted",
            "label.created",
            "label.deleted",
            "dependabot_alert.fixed",
        }

        if canonical in ignored_events:
            logger.info(
                "🤫 Ignoring event: %s",
                canonical.replace("_", " ").replace(".", " ").capitalize(),
            )
            return False

        if delivery_id in self._processed_deliveries:
            logger.debug(
                "🛡️  Suppressed duplicate delivery: %s",
                delivery_id,
            )
            return False

        sender = normalized.get("sender")
        if sender and sender.get("login") == BOT_LOGIN:
            logger.debug("🛡️  Suppressed bot-authored event: %s", delivery_id)
            return False

        raw = normalized.get("raw_payload", {})
        comment = raw.get("comment") or raw.get("review")
        if comment:
            user = comment.get("user") or {}
            if user.get("login") == BOT_LOGIN:
                logger.debug(
                    "🛡️  Suppressed bot-authored comment/review: %s", delivery_id
                )
                return False

        return True

    def process_event(self, data: dict[str, Any]) -> None:
        """The main entry point for processing a single normalized event."""
        delivery_id = data.get("delivery_id", "unknown")
        canonical = self.route_event(data)

        logger.info(
            "⚙️  Processing event: %s",
            canonical.replace("_", " ").replace(".", " ").capitalize(),
        )

        if not self.should_process_event(data):
            return

        self._processed_deliveries.add(delivery_id)

        # Token management
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

        repo_name = (
            data.get("repository", {}).get("full_name")
            if data.get("repository")
            else None
        )
        if not repo_name:
            logger.warning("⚠️  No repository found in event delivery: %s", delivery_id)
            return

        try:
            results = agent.run(data, repo_name)
            for r in results:
                status_symbol = "🤖" if r.success else "❌"
                logger.info(
                    "%s Agent action: %s",
                    status_symbol,
                    r.detail.replace("_", " ").replace(".", " ").capitalize(),
                )
        except Exception:
            logger.exception("💥 Agent core failed for repo %s", repo_name)
