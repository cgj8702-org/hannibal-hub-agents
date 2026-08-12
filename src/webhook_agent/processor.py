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
        logger.warning(
            "Invalid integer for %s=%r, using default %d", name, raw, default
        )
        return default


def _add_eyes_reaction(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Programmatically react with eyes emoji to incoming comment events."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload", {})
        repo = gh.get_repo(repo_name)

        if canonical.startswith("issue_comment."):
            issue_data = raw.get("issue", {})
            comment_data = raw.get("comment", {})
            issue_num = issue_data.get("number")
            comment_id = comment_data.get("id")
            if issue_num and comment_id:
                issue = repo.get_issue(issue_num)
                comment = issue.get_comment(comment_id)
                comment.create_reaction("eyes")
        elif canonical.startswith("pull_request_review_comment."):
            pr_data = raw.get("pull_request", {})
            comment_data = raw.get("comment", {})
            pr_num = pr_data.get("number")
            comment_id = comment_data.get("id")
            if pr_num and comment_id:
                pr = repo.get_pull(pr_num)
                comment = pr.get_review_comment(comment_id)
                comment.create_reaction("eyes")
    except Exception as exc:
        logger.warning("Failed to add eyes reaction to comment: %s", exc)


def _should_prefetch_diff(canonical: str, raw: dict[str, Any]) -> bool:
    """Determine if a PR diff pre-fetch is necessary for this event to avoid prompt bloat.

    Pre-fetching is restricted to PR creation/updates, review requests, and explicit
    review slash commands (/review, /audit, /test, /resolve).
    """
    if canonical in (
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.ready_for_review",
        "pull_request.reopened",
        "pull_request_review_requested",
    ):
        return True

    if canonical.startswith("issue_comment.") or canonical.startswith(
        "pull_request_review_comment."
    ):
        comment_body = (raw.get("comment", {}) or {}).get("body", "").lower()
        review_triggers = {
            "/review",
            "/audit",
            "/test",
            "/resolve",
            "/critique",
            "please review",
        }
        if any(trigger in comment_body for trigger in review_triggers):
            return True

    return False


def _prefetch_pr_diff(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Programmatically pre-fetch PR diff and inject into raw_payload for 1-turn review execution."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "pr_diff" in raw:
            return

        if not _should_prefetch_diff(canonical, raw):
            return

        pr_number = None
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")
        elif (
            "issue" in raw
            and isinstance(raw["issue"], dict)
            and raw["issue"].get("pull_request")
        ):
            pr_number = raw["issue"].get("number")

        if not pr_number:
            return

        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        diff_lines: list[str] = []
        for f in pr.get_files():
            patch = f.patch or "No patch available (binary/renamed/empty)."
            diff_lines.append(
                f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}"
            )

        if diff_lines:
            raw["pr_diff"] = "\n".join(diff_lines)
            logger.info(
                "Pre-fetched PR #%d diff (%d files) for 1-turn review",
                pr_number,
                len(diff_lines),
            )
    except Exception as exc:
        logger.warning("Could not pre-fetch PR diff: %s", exc)


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
        self.app_id = _env_int("GITHUB_APP_ID", 4133145)
        self.installation_id = _env_int("GITHUB_INSTALLATION_ID", 150411146)
        self.private_key_path = os.getenv(
            "GITHUB_PRIVATE_KEY_PATH", "/tmp/keys/github-app-private-key.pem"
        )

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
        * Comments mentioning @dependabot are suppressed.
        * The ``edited`` action is filtered out.
        * Automated CI noise events (check_suite, check_run, status) are suppressed.
        * Read-only PR lifecycle events (pull_request.closed) are suppressed.
        * pull_request.synchronize is NOT suppressed — it is handled by the agent
          via get_commit_diff for incremental reviews of newly pushed commits.
        """
        delivery_id = ev.get("delivery_id")
        if delivery_id in self._processed_deliveries:
            return False
        if _is_bot_event(ev):
            return False

        raw = ev.get("raw_payload") or {}
        comment = raw.get("comment") or {}
        comment_body = comment.get("body") or ""
        if "@dependabot" in comment_body.lower():
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

        dry_run = os.environ.get("DRY_RUN", "0") in ("1", "true", "True")
        if not dry_run:
            _add_eyes_reaction(gh, repo_name, payload)
            _prefetch_pr_diff(gh, repo_name, payload)

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
