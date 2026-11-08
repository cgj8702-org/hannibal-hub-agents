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

import contextlib
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from github import Auth, Github

from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.events import (
    SUPPRESSED_EVENT_NAMES,
    SUPPRESSED_PR_METADATA_ACTIONS,
    Tier1Status,
    filter_tier_0,
    handle_ci_completed,
    handle_tier_1_programmatic,
    is_actionable_issue_opened,
    review_if_ci_green,
    route_event,
    should_process_event,
)
from webhook_agent.github.credentials import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)
from webhook_agent.github.pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
)
from webhook_agent.review.ci_gate import (
    evaluate_ci,
    is_ci_gate_enabled,
    last_bot_review_sha,
)
from webhook_agent.review.fast_path import (
    evaluate_dependency_fast_path,
    is_base_branch_merge_sync,
)
from webhook_agent.review.formatter import (
    truncate_log_payload,
)

from .core.agent_definition import WebhookAgent
from .review.comment_poster import _add_eyes_reaction, _remove_eyes_reaction
from .state.review_checkpoint import review_checkpoint_manager
from .webhook_types import ActionResult


def _has_meaningful_writeback(results: list[ActionResult] | None) -> bool:
    """Return True if at least one public writeback action succeeded on GitHub."""
    if not results:
        return False
    writeback_tools = {
        "review",
        "add_comment",
        "create_issue",
        "resolve_review_thread",
        "proactive_reconciliation_summary",
    }
    for r in results:
        tool = getattr(r, "tool", "")
        success = getattr(r, "success", False)
        detail = str(getattr(r, "detail", "") or "")
        if tool in writeback_tools and success:
            if tool == "review" and detail.startswith(("Skipped:", "Error:", "Deferred:")):
                continue
            return True
    return False


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


# Re-exported module helpers (extracted to pr_context.py, fast_path.py, and review/comment_poster.py)
# Preserved for backwards compatibility with existing consumers and unit tests.
__all__ = [
    "SUPPRESSED_EVENT_NAMES",
    "SUPPRESSED_PR_METADATA_ACTIONS",
    "AgentCore",
    "Tier1Status",
    "WebhookProcessor",
    "_add_eyes_reaction",
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_remove_eyes_reaction",
    "_should_prefetch_diff",
    "evaluate_ci",
    "evaluate_dependency_fast_path",
    "generate_trace_id",
    "is_base_branch_merge_sync",
    "is_ci_gate_enabled",
    "last_bot_review_sha",
]


# ---------------------------------------------------------------------------
# LLM Agent Execution Lifecycle (Inlined from agent_core.py)
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Generate a unique hex trace ID for event execution."""
    return uuid.uuid4().hex


_MERGE_PR_RE = re.compile(
    r"Merge pull request #(\d+)|\(#(\d+)\)|(?:pull request|PR)\s*#?(\d+)",
    re.IGNORECASE,
)


def _extract_item_id(raw: dict[str, Any]) -> int | None:
    """Extract PR or issue number from any GitHub webhook payload variant."""
    if not isinstance(raw, dict):
        return None

    # 1. Direct pull_request or issue objects
    pr_data = raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request")
    if isinstance(pr_data, dict) and pr_data.get("number") is not None:
        try:
            return int(pr_data["number"])
        except (TypeError, ValueError):
            pass

    issue_data = raw.get("issue")
    if isinstance(issue_data, dict) and issue_data.get("number") is not None:
        try:
            return int(issue_data["number"])
        except (TypeError, ValueError):
            pass

    # 2. Top-level number
    if raw.get("number") is not None:
        try:
            return int(raw["number"])
        except (TypeError, ValueError):
            pass

    # 3. Check suite pull_requests list or head_commit merge message (e.g. merge commits into main)
    check_suite = raw.get("check_suite")
    if isinstance(check_suite, dict):
        suite_prs = check_suite.get("pull_requests") or []
        for p in suite_prs:
            if isinstance(p, dict) and p.get("number") is not None:
                try:
                    return int(p["number"])
                except (TypeError, ValueError):
                    pass

        head_commit = check_suite.get("head_commit")
        if isinstance(head_commit, dict):
            msg = str(head_commit.get("message") or "")
            m = _MERGE_PR_RE.search(msg)
            if m:
                cand = next((g for g in m.groups() if g is not None), None)
                if cand is not None:
                    try:
                        return int(cand)
                    except (TypeError, ValueError):
                        pass

    # 4. Push event head_commit or commits list
    head_commit = raw.get("head_commit")
    if isinstance(head_commit, dict):
        msg = str(head_commit.get("message") or "")
        m = _MERGE_PR_RE.search(msg)
        if m:
            cand = next((g for g in m.groups() if g is not None), None)
            if cand is not None:
                try:
                    return int(cand)
                except (TypeError, ValueError):
                    pass

    commits = raw.get("commits")
    if isinstance(commits, list):
        for c in commits:
            if isinstance(c, dict):
                msg = str(c.get("message") or "")
                m = _MERGE_PR_RE.search(msg)
                if m:
                    cand = next((g for g in m.groups() if g is not None), None)
                    if cand is not None:
                        try:
                            return int(cand)
                        except (TypeError, ValueError):
                            pass

    return None


def _extract_log_context(payload: dict[str, Any]) -> dict[str, str]:
    """Extract standardized structured context fields for logging."""
    raw = payload.get("raw_payload") or {}
    raw_repo = payload.get("repository")
    if not isinstance(raw_repo, dict) and isinstance(raw, dict):
        raw_repo = raw.get("repository")
    repo = raw_repo.get("full_name", "unknown") if isinstance(raw_repo, dict) else "unknown"

    event = str(payload.get("canonical") or payload.get("event_name") or "unknown")

    item_id = _extract_item_id(raw)
    item_str = str(item_id) if item_id is not None else "N/A"

    from webhook_agent.models.model_chain import get_active_model
    from webhook_agent.models.rate_limiter import _resolve_tier

    tier = _resolve_tier()
    model = get_active_model(payload)

    return {
        "repo": repo,
        "event": event,
        "tier": tier,
        "model": model,
        "item_id": item_str,
        "delivery_id": str(payload.get("delivery_id", "unknown")),
    }


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

    def __init__(self, gh_client: Any = None, dry_run: bool = False) -> None:
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

        core_logger.debug(
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

        def _resolve_default_key_path() -> str:
            xdg_runtime = os.getenv("XDG_RUNTIME_DIR")
            if xdg_runtime and os.path.isdir(xdg_runtime):
                return os.path.join(xdg_runtime, "hannibal-keys", "github-app-private-key.pem")
            home = os.getenv("HOME") or os.path.expanduser("~")
            return os.path.join(
                home, ".local", "share", "hannibal", "keys", "github-app-private-key.pem"
            )

        self.private_key_path = os.getenv(
            "GITHUB_PRIVATE_KEY_PATH",
            _resolve_default_key_path(),
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
    # Routing and Perimeter Filtering (delegated to webhook_agent.events)
    # ---------------------------------------------------------------------------
    def route_event(self, ev: dict[str, Any]) -> str:
        """Translate a GitHub event into a canonical internal event string."""
        return route_event(ev)

    def _is_actionable_issue_opened(self, ev: dict[str, Any]) -> bool:
        """Return True if an issues.opened event explicitly requests agent action."""
        return is_actionable_issue_opened(ev)

    def _filter_tier_0(self, ev: dict[str, Any]) -> tuple[bool, str]:
        """Apply Tier 0 pure perimeter drops (0 compute, 0 API calls)."""
        return filter_tier_0(
            ev,
            processed_deliveries=self._processed_deliveries,
            route_event_fn=self.route_event,
        )

    def should_process_event(self, ev: dict[str, Any]) -> bool:
        """Apply Tier 0 loop-avoidance, noise filtering, and duplication checks."""
        return should_process_event(
            ev,
            processed_deliveries=self._processed_deliveries,
            route_event_fn=self.route_event,
        )

    def _handle_ci_completed(self, payload: dict[str, Any]) -> None:
        """Start reviews that were held back while CI was running."""
        handle_ci_completed(self.gh, payload, self.process_event)

    def _review_if_ci_green(self, gh: Any, repo_name: str, number: int, head_sha: str) -> None:
        """Review PR ``number`` now if CI is fully green and the bot has not reviewed this commit."""
        review_if_ci_green(gh, repo_name, number, head_sha, self.process_event)

    def process_event(self, payload: dict[str, Any], *, ci_verified: bool = False) -> None:
        """Process a Pub/Sub payload.

        Logs the canonical event, filters duplication and bot events,
        and delegates execution to AgentCore.
        """
        event_key = self.route_event(payload)
        ctx = _extract_log_context(payload)
        if event_key == "unknown":
            logger.debug(
                "Unknown event (event_name=%s, delivery_id=%s)",
                payload.get("event_name"),
                payload.get("delivery_id"),
            )
        logger.debug(
            "Processing event: %s [repo=%s, delivery_id=%s]",
            event_key,
            ctx["repo"],
            ctx["delivery_id"],
        )
        if not self.should_process_event(payload):
            return

        delivery_id = payload.get("delivery_id", "unknown")
        if delivery_id != "unknown":
            self._processed_deliveries[delivery_id] = None
            # Bound the dict size to avoid unbounded memory growth (FIFO eviction).
            if len(self._processed_deliveries) > self._MAX_PROCESSED_DELIVERIES:
                oldest_delivery_id = next(iter(self._processed_deliveries))
                del self._processed_deliveries[oldest_delivery_id]

        logger.info(
            "🚀 Webhook processing started [repo=%s, event=%s, tier=%s, model=%s, item_id=%s, delivery_id=%s]",
            ctx["repo"],
            event_key,
            ctx["tier"],
            ctx.get("model", "unknown"),
            ctx["item_id"],
            ctx["delivery_id"],
            extra={
                "json_fields": {
                    "repo": ctx["repo"],
                    "event": event_key,
                    "tier": ctx["tier"],
                    "model": ctx.get("model", "unknown"),
                    "item_id": ctx["item_id"],
                    "delivery_id": ctx["delivery_id"],
                }
            },
        )

        # Set canonical event name in payload for AgentCore and WebhookAgent
        payload["canonical"] = event_key

        try:
            self._dispatch_event(payload, event_key, delivery_id, ci_verified=ci_verified)
        except Exception:
            if delivery_id != "unknown":
                self._processed_deliveries.pop(delivery_id, None)
            raise

    def _handle_tier_1_programmatic(
        self,
        payload: dict[str, Any],
        event_key: str,
        delivery_id: str,
        *,
        ci_verified: bool = False,
    ) -> Tier1Status:
        """Execute Tier 1 deterministic operations (0 LLM calls).

        Delegates to webhook_agent.events.tier1_router.handle_tier_1_programmatic.
        """
        dry_run = (os.environ.get("DRY_RUN", "0") in ("1", "true", "True")) or self.dry_run
        return handle_tier_1_programmatic(
            self.gh,
            payload,
            event_key,
            delivery_id,
            dry_run=dry_run,
            ci_verified=ci_verified,
            handle_ci_completed_fn=self._handle_ci_completed,
        )

    def _dispatch_tier_2_llm(
        self,
        payload: dict[str, Any],
        event_key: str,
        delivery_id: str,
    ) -> None:
        """Execute Tier 2 bounded LLM delegation."""
        gh = self.gh
        agent = self._get_agent_core()
        raw_repo = payload.get("repository")
        if not isinstance(raw_repo, dict) and isinstance(payload.get("raw_payload"), dict):
            raw_repo = payload["raw_payload"].get("repository")
        raw_repo_name = raw_repo.get("full_name") if isinstance(raw_repo, dict) else None
        if not raw_repo_name or not isinstance(raw_repo_name, str):
            logger.warning("No repository found in event payload for Tier 2 dispatch")
            return
        repo_name: str = raw_repo_name
        ctx = _extract_log_context(payload)
        dry_run = (os.environ.get("DRY_RUN", "0") in ("1", "true", "True")) or self.dry_run

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
        pr_number = (raw.get("pull_request") or {}).get("number") or (raw.get("issue") or {}).get(
            "number"
        )
        pr_num_int: int | None = None
        if pr_number is not None:
            try:
                pr_num_int = int(pr_number)
            except (TypeError, ValueError):
                pr_num_int = None
        else:
            logger.info(
                "Skipping Tier 2 dispatch: event '%s' has no associated PR or issue number for %s (delivery_id=%s)",
                canonical or payload.get("event_name", "unknown"),
                repo_name,
                delivery_id,
            )
            return
        head_sha = (pr_data.get("head") or {}).get("sha", "") if isinstance(pr_data, dict) else ""

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

        if not dry_run and not resumed_checkpoint:
            _prefetch_pr_diff(gh, repo_name, payload)
            _prefetch_inline_comment_context(gh, repo_name, payload)
            _prefetch_previous_bot_reviews(gh, repo_name, payload)

            if not head_sha and raw.get("diff_sha"):
                head_sha = str(raw["diff_sha"])
                payload["head_sha"] = head_sha
            elif not head_sha and raw.get("head_sha"):
                head_sha = str(raw["head_sha"])
                payload["head_sha"] = head_sha

            # Re-check closed/merged state if live prefetch discovered it was closed on GitHub
            if is_pr_event and pr_num_int is not None:
                updated_pr = (
                    raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
                )
                updated_state = (
                    (updated_pr.get("state") or "").lower() if isinstance(updated_pr, dict) else ""
                )
                if updated_state == "closed" or pr_closed_registry.is_closed(repo_name, pr_num_int):
                    logger.debug(
                        "🔒 PR %s#%s was closed or merged on GitHub; skipping agent execution.",
                        repo_name,
                        pr_number,
                    )
                    return

        # In-Flight Audit Deduplication claim key for release on failure
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
                from webhook_agent.state.reconciliation import (
                    build_reconciliation_cache_key,
                )

                audit_cache_key = build_reconciliation_cache_key(repo_name, pr_number, head_sha)

        # Guarded Eyes Reaction: only react once closed/merged PR and deduplication guardrails pass
        eyes_reaction = None
        if not dry_run:
            eyes_reaction = _add_eyes_reaction(gh, repo_name, payload)

        try:
            results = agent.run(payload, repo_name, gh_client=gh)
        except Exception:
            if audit_cache_key:
                from webhook_agent.state.reconciliation import (
                    release_reconciliation_claim,
                )

                release_reconciliation_claim(audit_cache_key)
            if not dry_run and eyes_reaction is not None:
                _remove_eyes_reaction(gh, repo_name, payload, reaction=eyes_reaction)
            raise

        if not dry_run and eyes_reaction is not None and not _has_meaningful_writeback(results):
            logger.info(
                "👀 No external action taken for %s; removing eyes reaction",
                payload.get("canonical", ""),
            )
            _remove_eyes_reaction(gh, repo_name, payload, reaction=eyes_reaction)

        # If the run failed (e.g. model unavailable, retries exhausted, agent crash),
        # release the claim and evict from processed deliveries so resume sweeps can retry.
        has_failure = bool(results and any(not getattr(r, "success", True) for r in results))
        if has_failure:
            if audit_cache_key:
                from webhook_agent.state.reconciliation import (
                    release_reconciliation_claim,
                )

                release_reconciliation_claim(audit_cache_key)
            if delivery_id != "unknown":
                self._processed_deliveries.pop(delivery_id, None)
        if results:
            tool_summary: dict[str, int] = {}
            for r in results:
                msg = getattr(r, "detail", None) or getattr(r, "message", str(r))
                status_symbol = "OK" if r.success else "FAIL"
                tool_name = getattr(r, "tool", "")
                tool_label = f" (tool: {tool_name})" if tool_name else ""
                raw_act_result = getattr(r, "action_result", None)
                if (
                    raw_act_result is None
                    and isinstance(msg, str)
                    and msg.startswith("tool executed: ")
                ):
                    import ast

                    with contextlib.suppress(Exception):
                        raw_act_result = ast.literal_eval(msg[len("tool executed: ") :])
                if raw_act_result is None:
                    raw_act_result = msg

                json_fields = {
                    "status": status_symbol,
                    "tool": tool_name,
                    "action_result": raw_act_result,
                    "repo": ctx["repo"],
                    "item_id": ctx["item_id"],
                    "event": ctx["event"],
                    "tier": ctx["tier"],
                    "model": ctx.get("model", "unknown"),
                    "delivery_id": ctx["delivery_id"],
                }
                # Individual tool results at DEBUG to reduce log volume;
                # a single aggregated summary is emitted at INFO below.
                logger.debug(
                    "Agent action [%s]%s: %s [repo=%s, item_id=%s]",
                    status_symbol,
                    tool_label,
                    truncate_log_payload(msg, 300),
                    ctx["repo"],
                    ctx["item_id"],
                    extra={"json_fields": json_fields},
                )
                tool_key = f"{tool_name}:{status_symbol}"
                tool_summary[tool_key] = tool_summary.get(tool_key, 0) + 1

            summary_str = ", ".join(
                f"{name}x{count}" if count > 1 else name for name, count in tool_summary.items()
            )
            logger.info(
                "✅ Webhook processing completed [repo=%s, event=%s, tier=%s, model=%s, item_id=%s, actions=%d, tools=%s]",
                ctx["repo"],
                ctx["event"],
                ctx["tier"],
                ctx.get("model", "unknown"),
                ctx["item_id"],
                len(results),
                summary_str,
                extra={
                    "json_fields": {
                        "repo": ctx["repo"],
                        "event": ctx["event"],
                        "tier": ctx["tier"],
                        "model": ctx.get("model", "unknown"),
                        "item_id": ctx["item_id"],
                        "delivery_id": ctx["delivery_id"],
                        "action_count": len(results),
                    }
                },
            )
        else:
            logger.info(
                "✅ Webhook processing completed with no actions taken [repo=%s, event=%s, tier=%s, model=%s, item_id=%s]",
                ctx["repo"],
                ctx["event"],
                ctx["tier"],
                ctx.get("model", "unknown"),
                ctx["item_id"],
                extra={
                    "json_fields": {
                        "repo": ctx["repo"],
                        "event": ctx["event"],
                        "tier": ctx["tier"],
                        "model": ctx.get("model", "unknown"),
                        "item_id": ctx["item_id"],
                        "delivery_id": ctx["delivery_id"],
                        "action_count": 0,
                    }
                },
            )

    def _dispatch_event(
        self,
        payload: dict[str, Any],
        event_key: str,
        delivery_id: str,
        *,
        ci_verified: bool = False,
    ) -> None:
        """Dispatch an event through Tier 1 (deterministic programmatic action) and Tier 2 (LLM delegation)."""
        tier1_status = self._handle_tier_1_programmatic(
            payload, event_key, delivery_id, ci_verified=ci_verified
        )
        if tier1_status != Tier1Status.ADVANCE:
            return

        self._dispatch_tier_2_llm(payload, event_key, delivery_id)
