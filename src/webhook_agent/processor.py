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

from .formatter import (
    truncate_log_payload,
)

logger = logging.getLogger("webhook_agent.processor")


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
        action = payload.get("action") or raw.get("action")
        if action == "deleted" or canonical.endswith(".deleted"):
            return

        if canonical.startswith("issue_comment."):
            issue_data = raw.get("issue", {})
            comment_data = raw.get("comment", {})
            issue_num = issue_data.get("number")
            comment_id = comment_data.get("id")
            if issue_num and comment_id:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(issue_num)
                comment = issue.get_comment(comment_id)
                comment.create_reaction("eyes")
        elif canonical.startswith("pull_request_review_comment."):
            pr_data = raw.get("pull_request", {})
            comment_data = raw.get("comment", {})
            pr_num = pr_data.get("number")
            comment_id = comment_data.get("id")
            if pr_num and comment_id:
                repo = gh.get_repo(repo_name)
                pr = repo.get_pull(pr_num)
                comment = pr.get_review_comment(comment_id)
                comment.create_reaction("eyes")
        elif canonical in ("pull_request.opened", "pull_request.reopened") or (
            canonical.startswith("pull_request.") and action in ("opened", "reopened")
        ):
            pr_data = raw.get("pull_request", {})
            pr_num = pr_data.get("number")
            if pr_num:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(pr_num)
                issue.create_reaction("eyes")
    except Exception as exc:
        logger.debug("Failed to add eyes reaction to comment: %s", exc)


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

        if (
            canonical == "pull_request.synchronize"
            or raw.get("action") == "synchronize"
        ):
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

        _prefetch_inline_comment_context(gh, repo_name, payload)
        _preexecute_resolve_command(gh, repo_name, payload)
        _prefetch_commit_history(gh, repo_name, payload)

    except Exception as exc:
        logger.debug("Could not pre-fetch PR diff: %s", exc)


def _prefetch_inline_comment_context(
    gh: Github, repo_name: str, payload: dict[str, Any]
) -> None:
    """Pre-fetch code context snippet for inline review comment events."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload")
        if (
            not isinstance(raw, dict)
            or not canonical.startswith("pull_request_review_comment.")
            or "inline_code_context" in raw
        ):
            return

        comment = raw.get("comment", {})
        path = comment.get("path")
        diff_hunk = comment.get("diff_hunk")
        line = comment.get("line") or comment.get("original_line")

        if path and (diff_hunk or line):
            raw["inline_code_context"] = (
                f"File: {path} (Line {line})\nDiff Hunk Snippet:\n{diff_hunk or 'N/A'}"
            )
            logger.info("Pre-fetched inline comment code context for %s:%s", path, line)
    except Exception as exc:
        logger.debug("Could not pre-fetch inline comment context: %s", exc)


def _preexecute_resolve_command(
    gh: Github, repo_name: str, payload: dict[str, Any]
) -> None:
    """Pre-execute conflict resolution programmatically on /resolve command."""
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "conflict_resolution_result" in raw:
            return

        comment_body = ""
        if "comment" in raw and isinstance(raw["comment"], dict):
            comment_body = raw["comment"].get("body") or ""

        if "/resolve" not in comment_body:
            return

        pr_number = None
        if "issue" in raw and isinstance(raw["issue"], dict):
            if raw["issue"].get("pull_request"):
                pr_number = raw["issue"].get("number")
        elif "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")

        if not pr_number:
            return

        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        from .tools.resolve_conflicts import (
            resolve_merge_conflicts,
        )

        res = resolve_merge_conflicts(
            pr_number=pr_number,
            head_branch=pr.head.ref,
            base_branch=pr.base.ref,
        )
        raw["conflict_resolution_result"] = res
        logger.info(
            "Pre-executed /resolve command for PR #%d (Success: %s)",
            pr_number,
            res.get("success"),
        )
    except Exception as exc:
        logger.debug("Could not pre-execute /resolve command: %s", exc)


def _prefetch_commit_history(
    gh: Github, repo_name: str, payload: dict[str, Any]
) -> None:
    """Pre-fetch commit history log summary for /create command."""
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "commit_history_summary" in raw:
            return

        body = ""
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            body = raw["pull_request"].get("body") or ""
        elif "comment" in raw and isinstance(raw["comment"], dict):
            body = raw["comment"].get("body") or ""

        if "/create" not in body:
            return

        pr_number = None
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")
        elif (
            "issue" in raw
            and isinstance(raw["issue"], dict)
            and raw["issue"].get("pull_request") is not None
        ):
            pr_number = raw["issue"].get("number")

        if not pr_number:
            return

        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        commit_summaries: list[str] = []
        for c in pr.get_commits():
            msg = (
                c.commit.message.splitlines()[0]
                if c.commit and c.commit.message
                else "No message"
            )
            sha = c.sha[:7] if c.sha else "N/A"
            author = c.author.login if c.author else "Unknown"
            commit_summaries.append(f"- `{sha}` ({author}): {msg}")

        if commit_summaries:
            raw["commit_history_summary"] = "\n".join(commit_summaries[:10])
            logger.info(
                "Pre-fetched commit history summary (%d commits) for /create PR #%d",
                len(commit_summaries),
                pr_number,
            )
    except Exception as exc:
        logger.debug("Could not pre-fetch commit history for /create: %s", exc)


def _prefetch_previous_bot_reviews(
    gh: Github, repo_name: str, payload: dict[str, Any]
) -> None:
    """Pre-fetch previous reviews posted by hannibal-hub-agents[bot]."""
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "previous_bot_reviews" in raw:
            return

        pr_number = None
        if "pull_request" in raw and isinstance(raw["pull_request"], dict):
            pr_number = raw["pull_request"].get("number")
        elif (
            "issue" in raw
            and isinstance(raw["issue"], dict)
            and raw["issue"].get("pull_request") is not None
        ):
            pr_number = raw["issue"].get("number")

        if not pr_number:
            return

        repo = gh.get_repo(repo_name)
        try:
            pr = repo.get_pull(pr_number)
        except Exception:
            return

        bot_reviews: list[str] = []
        for r in pr.get_reviews():
            login = (getattr(r.user, "login", "") or "").lower()
            if "hannibal-hub-agents" in login or (login and login.endswith("[bot]")):
                state = getattr(r, "state", "COMMENT")
                body_snippet = (r.body or "")[:300].replace("\n", " ")
                bot_reviews.append(f"- State: {state} | Body: {body_snippet}")

        if bot_reviews:
            raw["previous_bot_reviews"] = "\n".join(bot_reviews[-3:])
            logger.info(
                "Pre-fetched previous bot reviews (%d reviews) for PR #%d",
                len(bot_reviews),
                pr_number,
            )
    except Exception as exc:
        logger.debug("Could not pre-fetch previous bot reviews: %s", exc)


def _preexecute_implement_command(
    gh: Github, repo_name: str, payload: dict[str, Any]
) -> None:
    """Pre-process /implement or /feature commands on Issues and Issue comments."""
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict) or "implement_instruction" in raw:
            return

        body = ""
        issue_num = None
        if "issue" in raw and isinstance(raw["issue"], dict):
            issue_num = raw["issue"].get("number")
            body = raw["issue"].get("body") or ""

        if "comment" in raw and isinstance(raw["comment"], dict):
            body = raw["comment"].get("body") or ""

        if not body:
            return

        body_lower = body.lower()
        if "/implement" not in body_lower and "/feature" not in body_lower:
            return

        if not issue_num:
            return

        # Extract instruction after command
        instruction = body
        for cmd in ("/implement", "/feature"):
            if cmd in body_lower:
                idx = body_lower.find(cmd)
                instruction = body[idx + len(cmd) :].strip()
                break

        if not instruction:
            instruction = (raw.get("issue") or {}).get("title") or "Implement feature"

        raw["implement_instruction"] = f"Issue #{issue_num}: {instruction}"
        logger.info(
            "Pre-processed /implement command for Issue #%d: '%s'",
            issue_num,
            instruction[:40],
        )
    except Exception as exc:
        logger.debug("Could not pre-process /implement command: %s", exc)


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
        from logic.constants import (
            DEFAULT_GITHUB_APP_ID,
            DEFAULT_GITHUB_INSTALLATION_ID,
        )

        self.app_id = _env_int("GITHUB_APP_ID", int(DEFAULT_GITHUB_APP_ID))
        self.installation_id = _env_int(
            "GITHUB_INSTALLATION_ID", int(DEFAULT_GITHUB_INSTALLATION_ID)
        )
        self.private_key_path = os.getenv(
            "GITHUB_PRIVATE_KEY_PATH", "/tmp/keys/github-app-private-key.pem"
        )
        # Built lazily on first process_event() call — NOT here. Keeps
        # WebhookProcessor() cheap to construct for tests that only exercise
        # routing/filtering (see test_worker.py), and ensures the ADK
        # session/memory services inside WebhookAgent are constructed exactly
        # once and reused for the lifetime of this worker process, instead of
        # being discarded and rebuilt on every event.
        self._agent_core: AgentCore | None = None

    def _get_agent_core(self) -> AgentCore:
        if self._agent_core is None:
            self._agent_core = AgentCore(
                dry_run=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
            )
        return self._agent_core

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
            if (
                sender_login not in allowed_users
                and comment_author not in allowed_users
            ):
                return False

        event_name = ev.get("event_name")
        canonical = self.route_event(ev)
        # Suppress closed PR events and review submission events to prevent self-review feedback loops and reviews on closed PRs
        if canonical in ("pull_request.closed", "pull_request_review.submitted") or (
            event_name == "pull_request" and action == "closed"
        ):
            raw = ev.get("raw_payload") or {}
            repo = raw.get("repository") or {}
            repo_full_name = repo.get("full_name", "")
            pr_data = raw.get("pull_request") or {}
            pr_number = pr_data.get("number")
            if repo_full_name and pr_number:
                try:
                    from .cancellation import pr_closed_registry

                    pr_closed_registry.mark_closed(repo_full_name, int(pr_number))
                except Exception as ex:
                    logger.warning(
                        "Failed to mark PR %s#%s closed in registry: %s",
                        repo_full_name,
                        pr_number,
                        ex,
                    )
            return False

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

        agent = self._get_agent_core()

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
            _prefetch_inline_comment_context(gh, repo_name, payload)
            _preexecute_resolve_command(gh, repo_name, payload)
            _prefetch_commit_history(gh, repo_name, payload)
            _prefetch_previous_bot_reviews(gh, repo_name, payload)
            _preexecute_implement_command(gh, repo_name, payload)

        results = agent.run(payload, repo_name, gh_client=gh)
        if results:
            for r in results:
                msg = getattr(r, "detail", None) or getattr(r, "message", str(r))
                status_symbol = "OK" if r.success else "FAIL"
                logger.info(
                    "Agent action [%s]: %s",
                    status_symbol,
                    truncate_log_payload(msg, 300),
                )
        else:
            logger.info(
                "Agent completed execution with no actions taken for repo %s",
                repo_name,
            )
