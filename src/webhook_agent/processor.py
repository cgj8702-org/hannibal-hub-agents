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
from .bot_identity import _is_bot_event
from .github_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

logger = logging.getLogger("processor")


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
        raw = normalized.get("raw_payload", {})
        event_name = (
            normalized.get("event_name")
            or (raw.get("event_name") if isinstance(raw, dict) else "")
            or ""
        )
        action = (
            normalized.get("action")
            or (raw.get("action") if isinstance(raw, dict) else "")
            or ""
        )

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

        logger.debug(
            "Evaluating event filtering: delivery_id=%s, canonical=%s",
            delivery_id[-4:] if delivery_id else "none",
            canonical,
        )

        ignored_events = {
            "pull_request.closed",
            "pull_request.synchronize",
            "pull_request.edited",
            "check_suite.requested",
            "check_suite.completed",
            "issue_comment.deleted",
            "label.created",
            "label.deleted",
            "dependabot_alert.fixed",
        }

        if canonical in ignored_events:
            logger.debug(
                "🚫 Event '%s' matches ignored events list - will suppress",
                canonical,
            )
            logger.info(
                "🤫 Ignoring event: %s",
                canonical.replace("_", " ").replace(".", " ").capitalize(),
            )
            return False

        if delivery_id in self._processed_deliveries:
            logger.debug(
                "🔁 Duplicate delivery detected: %s (already in processed set)",
                delivery_id[-4:],
            )
            logger.debug(
                "🛡️  Suppressed duplicate delivery: %s",
                delivery_id[-4:],
            )
            return False

        if _is_bot_event(normalized):
            sender = normalized.get("sender", {})
            logger.debug(
                "🤖 Bot event check: sender=%s, will suppress if bot-originated",
                sender.get("login", "unknown"),
            )
            logger.debug(
                "🛡️  Suppressed bot-originated event: %s (delivery: %s)",
                canonical,
                delivery_id[-4:],
            )
            return False

        logger.debug(
            "✅ Event '%s' passed all filtering checks - will process",
            canonical,
        )
        return True

    def _fetch_pr_diff(
        self, gh: Github, repo_full_name: str, pr_number: int, raw_payload: dict
    ) -> None:
        """Fetch PR diff and add it to raw_payload for agent context.

        This mutates raw_payload in place with the diff content.
        """
        try:
            repo = gh.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)
            files = pr.get_files()
            diff_summary = []
            for f in files:
                diff_summary.append(
                    f"File: {f.filename} ({f.status})\nPatch:\n{f.patch}\n{'-' * 40}"
                )
            raw_payload["pr_diff"] = (
                "\n".join(diff_summary) if diff_summary else "No files changed."
            )
        except Exception:
            raw_payload["pr_diff"] = "Could not fetch PR diff"

    def process_event(self, data: dict[str, Any]) -> None:
        """The main entry point for processing a single normalized event."""
        delivery_id = data.get("delivery_id", "unknown")
        canonical = self.route_event(data)
        data["canonical"] = canonical

        logger.debug(
            "📥 Received processing request: delivery_id=%s, canonical=%s",
            delivery_id[-4:] if delivery_id != "unknown" else "unknown",
            canonical,
        )

        logger.info(
            "⚙️  Processing event: %s",
            canonical.replace("_", " ").replace(".", " ").capitalize(),
        )

        if not self.should_process_event(data):
            logger.debug(
                "⏭️  Event filtering stopped processing for delivery_id=%s",
                delivery_id[-4:] if delivery_id != "unknown" else "unknown",
            )
            return

        logger.debug("✅ Event passed filtering, proceeding with processing")

        self._processed_deliveries.add(delivery_id)
        logger.debug("📝 Added delivery_id=%s to processed set", delivery_id[-4:])

        # Token management
        inst_token = load_cached_token(self.installation_id)
        if inst_token is None:
            logger.debug("🔑 No cached token found, generating new installation token")
            pem = load_private_key(self.private_key_path)
            jwt_token = generate_jwt(self.app_id, pem)
            inst_token = get_installation_token(jwt_token, self.installation_id)
            save_cached_token(self.installation_id, inst_token)
            logger.debug("🔑 Saved new installation token to cache")
        else:
            logger.debug("🔑 Using cached installation token")

        gh = Github(auth=Auth.Token(inst_token.token))
        logger.debug("🔌 Created GitHub client")

        agent = AgentCore(
            gh_client=gh,
            dry_run=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
        )
        logger.debug(
            "🤖 Initialized AgentCore (dry_run=%s)",
            os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
        )

        repo_name = (
            data.get("repository", {}).get("full_name")
            if isinstance(data.get("repository"), dict)
            else (
                data.get("raw_payload", {}).get("repository", {}).get("full_name")
                if isinstance(data.get("raw_payload"), dict)
                and isinstance(data.get("raw_payload", {}).get("repository"), dict)
                else None
            )
        )
        if not repo_name:
            logger.warning(
                "⚠️  No repository found in event delivery: %s", delivery_id[-4:]
            )
            return

        logger.debug("📦 Target repository: %s", repo_name)

        # Pre-fetch PR diff for /create triggers to provide context to the agent
        raw = data.get("raw_payload", {})
        pr = raw.get("pull_request", {})
        pr_body = pr.get("body") or ""
        if "/create" in pr_body and canonical.startswith("pull_request."):
            pr_number = pr.get("number")
            if pr_number:
                logger.debug(
                    "📄 [/create] detected in PR body, fetching diff for PR #%d",
                    pr_number,
                )
                self._fetch_pr_diff(gh, repo_name, pr_number, raw)

        try:
            logger.info("🚀 Agent starting execution for repo %s", repo_name)
            results = agent.run(data, repo_name)
            logger.debug(
                "🏁 Agent run completed, processing %d results",
                len(results) if results else 0,
            )
            for r in results:
                status_symbol = "🤖" if r.success else "❌"
                logger.info(
                    "%s Agent action: %s",
                    status_symbol,
                    r.detail,
                )
        except Exception:
            logger.exception("💥 Agent core failed for repo %s", repo_name)

        if not results:
            logger.info(
                "🏁 Agent completed execution with no actions taken for repo %s",
                repo_name,
            )
        else:
            logger.debug(
                "📊 Agent produced %d total results for delivery_id=%s",
                len(results),
                delivery_id[-4:],
            )
