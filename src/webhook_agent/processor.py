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

from .bot_identity import _is_bot_event, is_jules_sender
from .cancellation import pr_closed_registry
from .formatter import (
    truncate_log_payload,
)
from .github_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)
from .logic.diff_filter import filter_review_diff
from .webhook_agent import WebhookAgent
from .webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.processor")
core_logger = logging.getLogger("webhook_agent.core")


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
            pr_data = raw.get("pull_request", {})
            issue_num = issue_data.get("number") or pr_data.get("number")
            comment_data = raw.get("comment", {})
            comment_id = comment_data.get("id")
            if issue_num and comment_id:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(int(issue_num))
                comment = issue.get_comment(int(comment_id))
                comment.create_reaction("eyes")
        elif canonical.startswith("pull_request_review_comment."):
            pr_data = raw.get("pull_request", {})
            comment_data = raw.get("comment", {})
            pr_num = pr_data.get("number")
            comment_id = comment_data.get("id")
            if pr_num and comment_id:
                repo = gh.get_repo(repo_name)
                pr = repo.get_pull(int(pr_num))
                pr_comment = pr.get_review_comment(int(comment_id))
                pr_comment.create_reaction("eyes")
        elif canonical in (
            "pull_request.opened",
            "pull_request.reopened",
            "issues.opened",
            "issues.reopened",
        ) or (
            canonical.startswith(("pull_request.", "issues.")) and action in ("opened", "reopened")
        ):
            target_num = (raw.get("pull_request") or {}).get("number") or (
                raw.get("issue") or {}
            ).get("number")
            if target_num:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(int(target_num))
                issue.create_reaction("eyes")
    except Exception as exc:
        logger.warning("Failed to add eyes reaction to comment: %s", exc)


def _should_prefetch_diff(canonical: str, raw: dict[str, Any]) -> bool:
    """Determine if a PR diff pre-fetch is necessary for this event to avoid prompt bloat.

    Pre-fetching is restricted to PR creation/updates, review requests, and explicit
    review intent triggers.
    """
    if canonical in (
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.ready_for_review",
        "pull_request.reopened",
        "pull_request_review_requested",
    ):
        return True

    if canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
        comment_body = (raw.get("comment", {}) or {}).get("body", "").lower()
        from .review.writeback_policy import REVIEW_INTENT_KEYWORDS

        if any(trigger in comment_body for trigger in REVIEW_INTENT_KEYWORDS):
            return True
        # Forward-fix: issue_comment on a PR (issue.pull_request set) that needs
        # reconciliation against prior bot reviews should also get diff context.
        # Otherwise the agent emits resolutions with no grounding and the
        # deterministic fallback has no pr_diff to validate against.
        issue = raw.get("issue") or {}
        if isinstance(issue, dict) and issue.get("pull_request") is not None:
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
        elif "issue" in raw and isinstance(raw["issue"], dict) and raw["issue"].get("pull_request"):
            pr_number = raw["issue"].get("number")

        if pr_number is None:
            return

        try:
            pr_num_int = int(pr_number)
        except (TypeError, ValueError):
            return

        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_num_int)

        # Check live PR state: if closed or merged, register in pr_closed_registry
        raw_state = getattr(pr, "state", None)
        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        is_merged = getattr(pr, "merged", None) is True
        if pr_state == "closed" or is_merged:
            pr_closed_registry.mark_closed(repo_name, pr_num_int)
            if "pull_request" in raw and isinstance(raw["pull_request"], dict):
                raw["pull_request"]["state"] = "closed"
                raw["pull_request"]["merged"] = True
            elif "issue" in raw and isinstance(raw["issue"], dict):
                raw["issue"]["state"] = "closed"
            logger.info(
                "🔒 Live GitHub check: PR %s#%d is closed or merged (state=%s, merged=%s); registered as closed",
                repo_name,
                pr_num_int,
                pr_state,
                is_merged,
            )
            return

        changed_files: list[str] = []
        diff_lines: list[str] = []
        for f in pr.get_files():
            changed_files.append(f.filename)
            patch = f.patch or "No patch available (binary/renamed/empty)."
            diff_lines.append(f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}")

        if diff_lines:
            raw["changed_files"] = changed_files
            raw_diff = "\n".join(diff_lines)
            filtered = filter_review_diff(raw_diff)
            raw["pr_diff"] = filtered.filtered_diff or raw_diff
            logger.info(
                "Pre-fetched PR #%d diff (%d files, %d kept, churn=%d) for 1-turn review",
                pr_num_int,
                len(diff_lines),
                len(filtered.kept_files),
                filtered.reviewable_lines,
            )

        if canonical == "pull_request.synchronize" or raw.get("action") == "synchronize":
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

        _prefetch_inline_comment_context(gh, repo_name, payload)

    except Exception as exc:
        logger.debug("Could not pre-fetch PR diff: %s", exc)


def _prefetch_inline_comment_context(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
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


def is_base_branch_merge_sync(gh: Github, repo_name: str, payload: dict[str, Any]) -> bool:
    """Check if a pull_request.synchronize event is an update from the base branch (e.g. merging main).

    When a PR branch is updated with the base branch (via GitHub's 'Update branch' button
    or `git merge main`), the head commit is a merge commit from the base branch.
    Such events should NOT trigger automated re-reviews or dismiss existing approvals.
    """
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict):
            return False

        canonical = payload.get("canonical", "")
        action = raw.get("action")
        if canonical != "pull_request.synchronize" and action != "synchronize":
            return False

        pr_data = raw.get("pull_request")
        if not isinstance(pr_data, dict):
            return False

        head = pr_data.get("head") or {}
        head_sha = head.get("sha") or raw.get("after")
        if not head_sha:
            return False

        base = pr_data.get("base") or {}
        base_ref = (base.get("ref") or "").lower()
        base_sha = base.get("sha") or ""
        head_ref = (head.get("ref") or "").lower()
        before_sha = raw.get("before") or ""

        # Fast-path: Check raw_payload commits if available
        commits = raw.get("commits")
        if isinstance(commits, list) and commits:
            for c in commits:
                if isinstance(c, dict) and c.get("id") == head_sha:
                    msg = (c.get("message") or "").strip().lower()
                    first_line = msg.splitlines()[0] if msg else ""
                    if any(
                        pat in first_line
                        for pat in (
                            f"merge branch '{base_ref}'",
                            f"merge branch '{base_ref}' of",
                            f"merge remote-tracking branch 'origin/{base_ref}'",
                            "merge https://github.com/",
                            f"into {head_ref}",
                        )
                    ):
                        return True

        repo = gh.get_repo(repo_name)
        commit = repo.get_commit(head_sha)
        parents = getattr(commit, "parents", []) or []
        if len(parents) < 2:
            return False

        commit_obj = getattr(commit, "commit", None)
        msg = (getattr(commit_obj, "message", "") or "").strip().lower()
        first_line = msg.splitlines()[0] if msg else ""

        merge_patterns = (
            f"merge branch '{base_ref}'",
            f"merge branch '{base_ref}' of",
            f"merge remote-tracking branch 'origin/{base_ref}'",
            "merge https://github.com/",
            "merge commit",
            f"into {head_ref}",
        )
        is_merge_msg = any(pat in first_line for pat in merge_patterns)

        parent_shas = [p.sha for p in parents if hasattr(p, "sha")]
        is_base_parent = base_sha in parent_shas or is_merge_msg

        return bool(
            is_merge_msg or (is_base_parent and (not before_sha or before_sha in parent_shas))
        )
    except Exception as exc:
        logger.debug("Could not verify base branch merge sync: %s", exc)
        return False


def _prefetch_previous_bot_reviews(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
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
        had_request_changes = False
        had_prior_findings = False
        all_prior_findings: list[dict[str, Any]] = []

        from webhook_agent.review.metadata import (
            get_actionable_findings,
            has_actionable_findings,
        )

        for r in pr.get_reviews():
            u = getattr(r, "user", None)
            login = (getattr(u, "login", "") or "").lower() if u else ""
            if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                state = getattr(r, "state", "COMMENT")
                if state == "CHANGES_REQUESTED":
                    had_request_changes = True
                body = (r.body or "").strip()
                if has_actionable_findings(body, state):
                    had_prior_findings = True
                findings = get_actionable_findings(body)
                if findings:
                    all_prior_findings.extend(findings)
                body_clean = body
                bot_reviews.append(f"Review (State: {state}):\n{body_clean}")

        raw["prior_reviews_had_request_changes"] = had_request_changes
        raw["prior_reviews_had_findings"] = had_prior_findings or had_request_changes
        raw["prior_actionable_findings"] = all_prior_findings
        if bot_reviews:
            raw["previous_bot_reviews"] = "\n\n---\n\n".join(bot_reviews)
            logger.info(
                "Pre-fetched previous bot reviews (%d reviews, had_request_changes=%s, had_findings=%s, findings_count=%d) for PR #%d",
                len(bot_reviews),
                had_request_changes,
                raw["prior_reviews_had_findings"],
                len(all_prior_findings),
                pr_number,
            )

    except Exception as exc:
        logger.debug("Could not pre-fetch previous bot reviews: %s", exc)


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
            "GITHUB_PRIVATE_KEY_PATH", "/tmp/keys/github-app-private-key.pem"
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

    def normalize_event_type(self, raw: dict[str, Any], headers: dict[str, str]) -> str:
        """Resolve a canonical event name from headers and payload."""
        event_name = headers.get("X-GitHub-Event") or headers.get("x-github-event")
        action = raw.get("action")
        mapping = {
            ("pull_request", "opened"): "pull_request.opened",
            ("pull_request", "reopened"): "pull_request.reopened",
            ("pull_request", "synchronize"): "pull_request.synchronize",
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
        if event_name and action:
            if canonical := mapping.get((str(event_name), str(action))):
                return canonical
        if action:
            return f"{event_name}.{action}"
        return "unknown"

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
        comment = raw.get("comment") or raw.get("review_comment") or {}
        comment_body = comment.get("body") or ""
        if "@dependabot" in comment_body.lower():
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
        return event_name not in ("check_suite", "check_run", "status", "installation")

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

        dry_run = os.environ.get("DRY_RUN", "0") in ("1", "true", "True")
        if not dry_run:
            _prefetch_pr_diff(gh, repo_name, payload)
            _prefetch_inline_comment_context(gh, repo_name, payload)
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

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

        # In-Flight Audit Deduplication: prevent concurrent runs across webhooks & proactive sweeps
        audit_cache_key: str | None = None
        if is_pr_event and pr_number is not None:
            head_sha = (
                (pr_data.get("head") or {}).get("sha", "") if isinstance(pr_data, dict) else ""
            )
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
                from .proactive_service import (
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
            try:
                from .logic.lockfile_validator import (
                    is_pure_dependency_pr,
                    render_deterministic_approval_markdown,
                    validate_lockfile_diff,
                )
                from .webhook_agent import _submit_formal_review

                sender_login = (
                    (payload.get("sender") or {}).get("login", "")
                    or (raw.get("sender") or {}).get("login", "")
                    or ""
                )
                head_branch = (pr_data.get("head") or {}).get("ref", "")
                changed_files = raw.get("changed_files") or []
                pr_diff = raw.get("pr_diff") or ""

                if is_pure_dependency_pr(sender_login, head_branch, changed_files):
                    logger.info(
                        "⚡ Evaluating deterministic fast-path for PR %s#%s (%d changed files)",
                        repo_name,
                        pr_number,
                        len(changed_files),
                    )
                    val_res = validate_lockfile_diff(pr_diff, changed_files)
                    if val_res.should_approve:
                        pr_num_int = int(pr_number)
                        repo = gh.get_repo(repo_name)
                        pr = repo.get_pull(pr_num_int)
                        approval_body = render_deterministic_approval_markdown(val_res, pr_num_int)
                        target_key = f"{repo_name}#{pr_number}"
                        status_msg, submitted = _submit_formal_review(
                            pr=pr,
                            body=approval_body,
                            event="APPROVE",
                            target_key=target_key,
                        )
                        logger.info(
                            "⚡ Fast-Path: Deterministically approved dependency PR %s#%s: %s (submitted=%s)",
                            repo_name,
                            pr_number,
                            status_msg,
                            submitted,
                        )
                        return
                    else:
                        logger.info(
                            "⚡ Fast-Path: Dependency PR %s#%s bypassed to full agent audit: %s (%s)",
                            repo_name,
                            pr_number,
                            val_res.summary,
                            val_res.rejection_reason,
                        )
            except Exception as fast_path_err:
                logger.warning(
                    "Deterministic fast-path evaluation encountered error, falling back to agent: %s",
                    fast_path_err,
                )

        try:
            results = agent.run(payload, repo_name, gh_client=gh)
        except Exception:
            if audit_cache_key:
                from .proactive_service import release_reconciliation_claim

                release_reconciliation_claim(audit_cache_key)
            raise
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
