"""ADK-powered webhook agent that replaces the Gemma planner.

This module defines the ADK agent with all GitHub tools as Python functions,
and provides a synchronous interface for the existing webhook pipeline.

The agent uses:
- Gemma-4-31b-it via ADK's Gemini model wrapper
- InMemoryMemoryService for in-memory conversation memory
- InMemorySessionService for per-PR conversation context
- Plain Python functions as tools (ADK auto-generates JSON schemas)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import CancelledError
from datetime import UTC, datetime
from typing import Any

from github import Github
from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.genai import types as genai_types
from google.genai.errors import ServerError as GenAIServerError

from .bot_identity import _is_bot_event
from .memory_service import InMemoryMemoryService
from .webhook_types import ActionResult

try:
    from logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        get_allowed_models,
        rpm_waiter,
    )
except ImportError:
    from ..logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        get_allowed_models,
        rpm_waiter,
    )

logger = logging.getLogger("webhook_agent")


# Persistent background event loop used to run ADK coroutines safely from
# synchronous callers. Using a single long-lived loop prevents repeatedly
# creating and closing event loops (which caused "Event loop is closed"
# errors when background transports attempted to schedule callbacks during
# loop shutdown). We run the loop in a daemon thread and schedule coroutines
# onto it via asyncio.run_coroutine_threadsafe.
_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_LOOP_THREAD: threading.Thread | None = None
_GENAI_CLIENT: object | None = None


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    global _BG_LOOP, _BG_LOOP_THREAD
    if _BG_LOOP and _BG_LOOP.is_running():
        return _BG_LOOP

    loop = asyncio.new_event_loop()

    def _loop_worker() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_loop_worker, name="adk-bg-loop", daemon=True)
    thread.start()
    # Wait briefly for the background thread to start the loop to avoid
    # a race where run_coroutine_threadsafe is called before run_forever()
    # begins. This prevents scheduling onto a non-running loop which can
    # manifest as transport/loop shutdown races in httpx/anyio.
    start_deadline = time.time() + 2.0
    while not loop.is_running() and time.time() < start_deadline:
        time.sleep(0.01)
    _BG_LOOP = loop
    _BG_LOOP_THREAD = thread
    return _BG_LOOP


def run_in_bg_loop(coro: asyncio.coroutines) -> Any:
    """Schedule coroutine on the background loop and wait for result."""
    loop = _ensure_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        # Wait for result; use a reasonably long timeout to avoid hanging
        # the caller indefinitely if the remote model call stalls.
        return future.result(timeout=300)
    except CancelledError:
        raise
    except Exception:
        # Re-raise after logging to make debugging easier in logs
        logger.exception("Error running coroutine in background loop")
        raise


async def _create_genai_client_async(api_key: str):
    """Create a google.genai Client on the background loop thread."""
    from google.genai import Client

    # Construct the client synchronously on the background loop to bind any
    # async transports to that loop's lifecycle.
    return Client(api_key=api_key)


def get_shared_genai_client() -> object | None:
    """Return a process-wide cached google.genai Client, creating it on the
    background loop if needed. Returns None if no API key is configured.
    """
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT

    try:
        api_key = get_active_api_key()
    except Exception:
        api_key = None

    if not api_key:
        logger.debug("No active GenAI API key available to construct shared client")
        return None

    try:
        # Create client on background loop so httpx/anyio transports attach to
        # the long-lived loop rather than ephemeral per-event loops.
        client = run_in_bg_loop(_create_genai_client_async(api_key))
        _GENAI_CLIENT = client
        logger.info("Shared GenAI client created and cached on background loop")
        return _GENAI_CLIENT
    except Exception as exc:
        logger.exception("Failed to create shared GenAI client: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Template Loading Utility
# ---------------------------------------------------------------------------


def _load_template(filename: str) -> str:
    """Load a template file from the templates directory."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", filename)
    try:
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Template not found at %s", template_path)
        return ""


def _sanitize_pr_body(body: str) -> str:
    """Programmatically strip raw template instruction headers and placeholders from PR bodies."""
    if not body:
        return body

    lines = body.splitlines()
    sanitized: list[str] = []

    forbidden_exact = {
        "# 🤖 Pull Request Description Template",
        "# 📋 Title Format",
        "## 📋 Title Format",
        "Use this template when creating or editing pull request descriptions in the Hannibal Hub Agents repository.",
        "Use this template when creating or editing pull request descriptions.",
        "*[Check the boxes that apply, bestie!]*",
        "*[Provide a clear summary of the change. Don't just tell us WHAT you changed, tell us WHY. What was the root cause? Why is this the right solution?]*",
        "*[For AI contributors: Please provide a clinical audit of your process.]*",
        "*[Clinical-grade validation time! Please ensure all of these are checked before requesting a review.]*",
    }

    for line in lines:
        stripped = line.strip()
        if stripped in forbidden_exact:
            continue
        if stripped.startswith("[type] Brief description"):
            continue
        if stripped.startswith(("**Types:**", "- `feat:` New feature")):
            continue

        sanitized.append(line)

    result = "\n".join(sanitized)
    result = re.sub(r"^(?:---|\s+)+", "", result).strip()
    return result


def _fetch_repo_pr_template(
    gh: Any, repo_name: str, changed_files: list[str] | None = None
) -> str:
    """Fetch the target repository's custom PR template via PyGithub based on git diff analysis."""
    try:
        repo = gh.get_repo(repo_name)

        # Check if PULL_REQUEST_TEMPLATE directory exists for multi-template repos (e.g. hannibal-hub)
        try:
            templates = repo.get_contents(".github/PULL_REQUEST_TEMPLATE")
            if isinstance(templates, list):
                template_map = {t.name: t for t in templates}
                is_dev_only = False
                if changed_files:
                    is_dev_only = all(
                        f.startswith(("dev/", "scripts/", "docs/", ".github/"))
                        for f in changed_files
                    )

                target_file = (
                    "dev_pull_request_template.md"
                    if is_dev_only and "dev_pull_request_template.md" in template_map
                    else "prod_pull_request_template.md"
                )
                if target_file in template_map:
                    content_file = template_map[target_file]
                    if hasattr(content_file, "decoded_content"):
                        return content_file.decoded_content.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch template file from map: %s", exc)

        candidate_paths = [
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/pull_request_template.md",
        ]
        for path in candidate_paths:
            try:
                content_file = repo.get_contents(path)
                if hasattr(content_file, "decoded_content"):
                    return content_file.decoded_content.decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not fetch remote template path %s: %s", path, exc)
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch remote PR template for %s: %s", repo_name, exc)

    return _load_pr_template()


def _load_pr_template() -> str:
    """Load the PR description template from the templates directory."""
    return _load_template("pr_template.md")


def _load_code_review_template() -> str:
    """Load the code review template from the templates directory."""
    return _load_template("code_review_template.md")


# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

# ---------------------------------------------------------------------------
# Input Token Safety Limits (Capped to stay under token budget)
# ---------------------------------------------------------------------------
MAX_INPUT_TOKENS = 3500  # Default 3.5k tokens cap for Free Tier / Gemma models


def get_max_input_tokens() -> int:
    """Return max token input budget based on active tier capability.

    Free Tier / Gemma models: 3,500 tokens.
    Paid Tier Gemini Flash models: 35,000 tokens (10x context window).
    """
    tier = _resolve_tier()
    if tier == "paid":
        return 35000
    return MAX_INPUT_TOKENS


def count_tokens_exact(
    contents: str | list[Any], model_name: str = "gemini-3.5-flash-lite"
) -> int | None:
    """Count input tokens using Google GenAI SDK's client.models.count_tokens().

    Returns exact token count from the API if credentials are configured,
    or None if credentials fail or method is unavailable.
    """
    try:
        client = get_shared_genai_client()
        if client is None:
            return None

        # The Client.models.count_tokens may be sync; call directly. If the
        # underlying client exposes an async API, those calls will still be
        # bound to transports created on the background loop thanks to
        # constructing the client there.
        res = client.models.count_tokens(
            model=model_name,
            contents=contents if isinstance(contents, list) else [contents],
        )
        return getattr(res, "total_tokens", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("count_tokens API call skipped/unavailable: %s", exc)
        return None


def _truncate_text_to_token_limit(
    text: str,
    max_tokens: int | None = None,
    model_name: str = "gemma-4-31b-it",
    label: str = "Input",
) -> str:
    """Truncate input text to guarantee it stays strictly under max_tokens limit.

    Uses google.genai client.models.count_tokens() for exact measurement when available,
    falling back to character estimation (~3.5 chars/token).
    """
    if not text:
        return text

    if max_tokens is None:
        max_tokens = get_max_input_tokens()

    # Step 1: Try exact token count via google.genai API
    exact_count = count_tokens_exact(text, model_name=model_name)

    if exact_count is not None:
        if exact_count <= max_tokens:
            return text

        # Oversized payload: iteratively truncate to fit exact token limit
        current_text = text
        current_tokens = exact_count
        while current_tokens > max_tokens and len(current_text) > 100:
            target_ratio = (max_tokens - 300) / current_tokens
            new_length = max(100, int(len(current_text) * target_ratio))
            current_text = current_text[:new_length]
            new_count = count_tokens_exact(current_text, model_name=model_name)
            if new_count is None or new_count >= current_tokens:
                current_text = current_text[: int(len(current_text) * 0.8)]
                current_tokens = int(current_tokens * 0.8)
            else:
                current_tokens = new_count

        omitted_chars = len(text) - len(current_text)
        return (
            f"{current_text}\n\n"
            f"[⚠️ {label} truncated: reduced to {current_tokens} tokens "
            f"(omitted {omitted_chars} characters) to stay within {max_tokens} token limit]"
        )

    # Step 2: Fallback character estimation if API is offline/unauthenticated
    max_chars = max_tokens * 3  # Conservative limit
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    omitted = len(text) - max_chars
    return (
        f"{truncated}\n\n"
        f"[⚠️ {label} truncated: omitted {omitted} characters (~{omitted // 4} tokens) "
        f"to stay within {max_tokens} token limit]"
    )


# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


# Retry configuration for transient server errors
_MAX_RETRIES = int(os.environ.get("GEMMA_MODEL_MAX_RETRIES", "5"))


# ---------------------------------------------------------------------------
# Process-Wide Model Depletion Registry
# ---------------------------------------------------------------------------


class DepletedModelRegistry:
    """Tracks models that have hit 429 quota exhaustion to bypass them process-wide across events.

    Dynamically sets cooldown based on error metric:
    - RPD (Requests Per Day): 24 Hours (86,400s)
    - RPM / TPM (Requests/Tokens Per Minute): 60 Seconds
    - Default Fallback: 1 Hour (3,600s)
    """

    def __init__(self, default_cooldown: float = 3600.0) -> None:
        self.default_cooldown = default_cooldown
        self._depleted: dict[str, tuple[float, float]] = {}

    def mark_depleted(self, model_name: str, error: Exception | None = None) -> None:
        cooldown = self.default_cooldown
        metric_type = "DEFAULT (1h)"

        if error is not None:
            err_str = str(error).lower()
            if "perday" in err_str or "dayperproject" in err_str:
                cooldown = 86400.0  # 24 Hours for RPD daily limit
                metric_type = "RPD (24h)"
            elif (
                "perminute" in err_str
                or "minuteperproject" in err_str
                or "tokensperminute" in err_str
            ):
                cooldown = 60.0  # 60 Seconds for RPM/TPM minute limit
                metric_type = "RPM/TPM (60s)"

        self._depleted[model_name] = (time.time(), cooldown)
        logger.warning(
            "🔴 Model '%s' marked DEPLETED [%s] across process",
            model_name,
            metric_type,
        )

    def is_depleted(self, model_name: str) -> bool:
        if model_name not in self._depleted:
            return False
        timestamp, cooldown = self._depleted[model_name]
        if time.time() - timestamp > cooldown:
            del self._depleted[model_name]
            logger.info(
                "🟢 Model '%s' depletion cooldown expired (%ds), restored to pool",
                model_name,
                int(cooldown),
            )
            return False
        return True

    def filter_chain(self, chain: list[str]) -> list[str]:
        return [m for m in chain if not self.is_depleted(m)]


try:
    from logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        get_allowed_models,
        rpm_waiter,
    )
    from logic.firestore_registry import (
        firestore_depleted_registry as _DEPLETED_MODEL_REGISTRY,
    )
except ImportError:
    from ..logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        get_allowed_models,
        rpm_waiter,
    )

    _DEPLETED_MODEL_REGISTRY = DepletedModelRegistry(default_cooldown=3600.0)

logger = logging.getLogger("webhook_agent")


def get_model_chain() -> list[str]:
    """Build ordered list of fallback models sorted by TPM (Tokens/Min) descending.

    Filters out models currently marked as depleted in _DEPLETED_MODEL_REGISTRY,
    and restricts candidates to models allowed on the active tier.
    """
    active_tier = _resolve_tier()
    allowed = set(get_allowed_models(active_tier))
    default_primary = (
        "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.6-flash"
    )
    primary = os.environ.get("GEMMA_MODEL", default_primary)
    chain = [
        primary,
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
    ]
    seen: set[str] = set()
    deduped = [m for m in chain if m in allowed and not (m in seen or seen.add(m))]
    available = _DEPLETED_MODEL_REGISTRY.filter_chain(deduped)
    return available if available else deduped


def _select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type, active tier, and content commands.

    On Free Tier, defaults primary to gemini-3.5-flash-lite (500 RPD) to protect gemini-3.6-flash (20 RPD).
    Routes heavy workloads (pull_request.opened, slash commands, @mentions)
    to the primary model, and routine lifecycle events to the lightweight model.
    """
    active_tier = _resolve_tier()
    default_primary = (
        "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.6-flash"
    )
    primary = os.environ.get("GEMMA_MODEL", default_primary)
    lightweight = os.environ.get("GEMMA_LIGHTWEIGHT_MODEL", "gemini-3.5-flash-lite")

    if os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", "1") not in (
        "1",
        "true",
        "True",
    ):
        target = primary
    else:
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})

        if canonical in ("pull_request.opened", "pull_request.synchronize"):
            target = primary
        elif canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
            comment_body = ""
            if isinstance(raw.get("comment"), dict):
                comment_body = raw["comment"].get("body") or ""

            commands = (
                "/review",
                "/create",
                "/resolve",
                "/help",
                "@hannibal-hub-agents",
            )
            if any(cmd in comment_body for cmd in commands):
                target = primary
            else:
                target = lightweight
        else:
            target = lightweight

    if _DEPLETED_MODEL_REGISTRY.is_depleted(target):
        chain = get_model_chain()
        target = chain[0] if chain else target

    return target


_FALLBACK_MODEL = os.environ.get("GEMMA_MODEL_FALLBACK", "gemini-3.5-flash-lite")


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried.

    Transient errors include server unavailability (503), rate limiting (429),
    RESOURCE_EXHAUSTED errors, and other temporary issues.
    """
    if isinstance(error, GenAIServerError):
        error_code = getattr(error, "code", None)
        return error_code in (503, 500, 429, 502, 504)
    err_str = str(error).lower()
    err_type = type(error).__name__.lower()
    return (
        "429" in err_str
        or "resource_exhausted" in err_str
        or "resourceexhausted" in err_type
        or "clienterror" in err_type
    )


# ---------------------------------------------------------------------------
# ADK Tool Functions — API-Aligned Primitives
# Each function becomes an ADK tool automatically. The docstring and type
# hints define the JSON schema that Gemma sees.
#
# Tools are organized by GitHub API surface:
#   Files API:  read_file, write_file
#   Issues API: get_issue, update_issue  (PRs are issues in GitHub's API)
#   Pulls API:  open_pr, merge_pr, review
# ---------------------------------------------------------------------------


def _get_gh_from_ctx(ctx: Context) -> Github:
    """Retrieve the Github client from the agent context."""
    gh = ctx.state.get("gh_client") or ctx.state.get("user:gh_client")
    if gh is None:
        raise RuntimeError("GitHub client not found in agent context")
    return gh


def _get_repo_full_name(ctx: Context) -> str:
    """Retrieve the repo full name from the agent context."""
    name = ctx.state.get("repo_full_name") or ctx.state.get("user:repo_full_name")
    if name is None:
        raise RuntimeError("repo_full_name not found in agent context")
    return name


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------


def read_file(ctx: Context, file_path: str, ref: str | None = None) -> str:
    """Read a file from the repository at a specific git ref.

    Args:
        file_path: Path to the file in the repository.
        ref: Branch name, tag, or commit SHA. Defaults to the repo default branch.

    Returns:
        The file content string, token-capped.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        if ref is not None:
            kwargs["ref"] = ref
        content_file = repo.get_contents(file_path, **kwargs)
        if isinstance(content_file, list):
            return f"Error: '{file_path}' is a directory, not a file."
        decoded = content_file.decoded_content.decode("utf-8", errors="replace")
        return decoded
    except Exception as e:  # noqa: BLE001
        return f"Error reading file: {e}"


def write_file(
    ctx: Context,
    branch: str,
    file_path: str,
    content: str,
    message: str,
    base_branch: str | None = None,
) -> str:
    """Create or update a file on a branch with a commit message.

    Args:
        branch: Target branch to commit to. Created from base_branch if it does not exist.
        file_path: Path to the file in the repository.
        content: Complete file content to write.
        message: Commit message describing the change.
        base_branch: Branch to create the target from if it does not exist.

    Returns:
        A string describing the commit result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        base = base_branch or repo.default_branch
        branch_created = False
        try:
            repo.get_branch(branch)
        except Exception:  # noqa: BLE001
            sb = repo.get_branch(base)
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sb.commit.sha)
            branch_created = True

        try:
            repo.create_file(file_path, message, content, branch=branch)
        except Exception:  # noqa: BLE001
            existing = repo.get_contents(file_path, ref=branch)
            repo.update_file(file_path, message, content, existing.sha, branch=branch)
        status = f"Committed '{file_path}' to {branch}"
        if branch_created:
            status += f" (branch created from {base})"
        return status
    except Exception as e:  # noqa: BLE001
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# Issues API (PRs are issues in GitHub's API)
# ---------------------------------------------------------------------------


def get_issue(ctx: Context, number: int, include_diff: bool = False) -> str:
    """Get metadata for an issue or pull request.

    Args:
        number: Issue or PR number.
        include_diff: If true and the item is a PR, include file diffs and mergeability.

    Returns:
        Structured metadata. For PRs includes title, state, branches, mergeable
        status, and changed files. If include_diff is true, also includes
        file patches.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        parts: list[str] = [
            f"#{number}: {issue.title}",
            f"State: {issue.state}",
            f"Labels: {', '.join(lbl.name for lbl in issue.labels) or 'none'}",
        ]

        try:
            pr = repo.get_pull(number)
            is_pr = True
        except Exception:  # noqa: BLE001
            is_pr = False

        if is_pr:
            parts.append("Type: Pull Request")
            parts.append(f"Head: {pr.head.ref}")
            parts.append(f"Base: {pr.base.ref}")
            parts.append(f"Mergeable: {pr.mergeable}")
            parts.append(f"Mergeable state: {pr.mergeable_state}")
            parts.append(f"Changed files: {pr.changed_files}")
            parts.append(f"Additions: +{pr.additions}  Deletions: -{pr.deletions}")

            if include_diff:
                files = pr.get_files()
                diff_lines: list[str] = []
                for f in files:
                    patch = f.patch or "No patch available (binary/renamed/empty)."
                    diff_lines.append(
                        f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}"
                    )
                diff_text = "\n".join(diff_lines) if diff_lines else "No files changed."
                parts.append(f"\nDiff:\n{diff_text}")

        else:
            parts.append("Type: Issue")
            body_preview = (issue.body or "")[:500]
            if body_preview:
                parts.append(f"Body: {body_preview}")

        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        return f"Error fetching issue/PR: {e}"


def get_commit_diff(ctx: Context, base_sha: str, head_sha: str) -> str:
    """Fetch incremental code diff between two commits for PR updates.

    Args:
        base_sha: Base commit SHA (e.g. the PR's previous head before a push).
        head_sha: Head commit SHA (e.g. the PR's new head after a push).

    Returns:
        A string describing the incremental diff between the two commits.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        comparison = repo.compare(base_sha, head_sha)
        diff_lines = [f"Incremental Diff ({base_sha[:7]}..{head_sha[:7]}):\n"]
        for f in comparison.files:
            diff_lines.append(
                f"File: {f.filename} ({f.status})\nPatch:\n{f.patch or 'No patch available.'}\n{'-' * 40}"
            )
        return "\n".join(diff_lines)
    except Exception as e:  # noqa: BLE001
        return f"Error fetching commit diff: {e}"


# ---------------------------------------------------------------------------
# Rate Limiting & Safety Guardrails
# ---------------------------------------------------------------------------


class CommentRateLimiter:
    """Sliding window rate limiter to prevent comment spam per issue/PR."""

    def __init__(self, max_comments: int = 3, window_seconds: float = 60.0) -> None:
        self.max_comments = max_comments
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, target_key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        timestamps = [t for t in self._history.get(target_key, []) if t > cutoff]
        self._history[target_key] = timestamps
        return len(timestamps) < self.max_comments

    def record(self, target_key: str) -> None:
        now = time.time()
        if target_key not in self._history:
            self._history[target_key] = []
        self._history[target_key].append(now)


_COMMENT_RATE_LIMITER = CommentRateLimiter(max_comments=3, window_seconds=60.0)


def update_issue(
    ctx: Context,
    number: int,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    labels: list[str] | None = None,
) -> str:
    """Update an issue or pull request metadata (title, body, state, labels).

    For posting discussion comments, use add_comment() instead.

    Args:
        number: Issue or PR number.
        title: Update the title.
        body: Update the body or description.
        state: Set state to open or closed.
        labels: List of label names to add.

    Returns:
        A string summarizing all actions taken.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        actions: list[str] = []

        edit_kwargs: dict[str, Any] = {}
        if title is not None:
            edit_kwargs["title"] = title
        if body is not None:
            edit_kwargs["body"] = _sanitize_pr_body(body)
        if state is not None:
            edit_kwargs["state"] = state
        if edit_kwargs:
            issue.edit(**edit_kwargs)
            actions.append(f"Updated: {', '.join(edit_kwargs.keys())}")

        if labels:
            issue.add_to_labels(*labels)
            actions.append(f"Labels added: {labels}")

        return (
            f"#{number}: " + "; ".join(actions) if actions else f"#{number}: no changes"
        )
    except Exception as e:  # noqa: BLE001
        return f"Error updating issue/PR: {e}"


def add_comment(ctx: Context, issue_number: int, body: str) -> str:
    """Post a standard discussion comment on an issue or PR conversation thread.

    This does NOT trigger a code review or edit the issue/PR description.
    If a code review report is detected in the body, it is automatically
    redirected to the review() tool.

    Args:
        issue_number: Issue or PR number.
        body: Comment body (Markdown).

    Returns:
        A string describing the result.
    """
    # Programmatic Guardrail: Redirect code review reports erroneously sent to add_comment to review()
    if (
        "Code Review Report" in body
        or "| Category" in body
        or "**Scorecard**" in body
        or "## 4. Verdict Determination" in body
    ):
        logger.warning(
            "Redirecting code review report from add_comment() to review() for #%d",
            issue_number,
        )
        return review(ctx, pr_number=issue_number, body=body, event="COMMENT")

    repo_name = _get_repo_full_name(ctx)
    target_key = f"{repo_name}#{issue_number}"
    if not _COMMENT_RATE_LIMITER.is_allowed(target_key):
        return (
            f"Error: Comment rate limit exceeded for #{issue_number} "
            f"(max 3 comments per minute per thread)."
        )

    gh = _get_gh_from_ctx(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        c = issue.create_comment(body=body)
        _COMMENT_RATE_LIMITER.record(target_key)
        return f"Commented on #{issue_number}: {c.html_url}"
    except Exception as e:  # noqa: BLE001
        return f"Error commenting on issue/PR: {e}"


# ---------------------------------------------------------------------------
# Pulls API (PR-specific extensions)
# ---------------------------------------------------------------------------


def open_pr(
    ctx: Context,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str = "",
) -> str:
    """Open a new pull request.

    Args:
        head_branch: Head branch name.
        base_branch: Base branch name.
        title: Pull request title.
        body: Pull request body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    body = _sanitize_pr_body(body)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
        )
        return f"Opened PR #{pr.number} {pr.html_url}"
    except Exception as e:  # noqa: BLE001
        return f"Error opening PR: {e}"


def update_branch_from_base(ctx: Context, pr_number: int) -> str:
    """Update a pull request's head branch with the latest changes from its base branch.

    Uses GitHub's native branch update API (equivalent to clicking 'Update branch').
    Use this when a PR is out of date or has merge conflicts with the base branch.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the update result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        updated = pr.update_branch()
        if updated:
            return f"Successfully updated PR #{pr_number} branch '{pr.head.ref}' with latest changes from '{pr.base.ref}'."
        return f"PR #{pr_number} branch '{pr.head.ref}' is already up to date with '{pr.base.ref}'."
    except Exception as e:  # noqa: BLE001
        return (
            f"Error updating PR #{pr_number} branch: {e}. "
            f"If there are complex merge conflicts, notify the user that manual local rebase is required."
        )


def merge_pr(ctx: Context, pr_number: int, merge_method: str = "merge") -> str:
    """Merge a pull request with safety checks.

    Args:
        pr_number: Pull request number.
        merge_method: Merge method. One of merge, squash, rebase.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        # Safety Check 1: Mergeability & conflicts
        if pr.mergeable is False:
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Mergeable state is '{pr.mergeable_state}' (conflicts or dirty state)."
            )

        # Safety Check 2: CI Status Checks — GitHub computes `mergeable_state`
        # server-side and it already reflects required CI check results. We rely on
        # it instead of commit.get_combined_status() because that endpoint returns
        # 403 "Resource not accessible by integration" for the GitHub App
        # installation (no status-read permission).
        if pr.mergeable_state in ("blocked", "dirty"):
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Mergeable state is '{pr.mergeable_state}' "
                "(blocked by failing/pending required CI checks or conflicts)."
            )

        # Safety Check 3: Blocking Reviews
        reviews = pr.get_reviews()
        latest_reviews: dict[str, str] = {}
        for r in reviews:
            if r.user and r.user.login:
                latest_reviews[r.user.login] = r.state

        if any(state == "CHANGES_REQUESTED" for state in latest_reviews.values()):
            blocking = [
                user
                for user, state in latest_reviews.items()
                if state == "CHANGES_REQUESTED"
            ]
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Active CHANGES_REQUESTED reviews from: {', '.join(blocking)}."
            )

        res = pr.merge(merge_method=merge_method)
        return f"Merged: {res}"
    except Exception as e:  # noqa: BLE001
        return f"Error merging PR: {e}"


def _parse_scorecard_scores(body: str) -> list[int]:
    """Extract numeric scores from the scorecard table in a review body.

    Looks for the pattern '|  N  |' where N is 1-5 in the scorecard rows.
    Returns a list of parsed integer scores, or empty list if none found.
    """
    import re

    scores: list[int] = []
    for match in re.finditer(r"\|\s*\*\*[^*]+\*\*\s*\|\s*(\d)\s*\|", body):
        score = int(match.group(1))
        if 1 <= score <= 5:
            scores.append(score)
    return scores


def _parse_confidence(body: str) -> int | None:
    """Extract the confidence self-assessment score from a review body.

    Looks for 'My Confidence:' followed by a number 1-5.
    Returns the score or None if not found.
    """
    match = re.search(r"\*\*My Confidence:\*\*\s*(\d)", body)
    if match:
        val = int(match.group(1))
        if 1 <= val <= 5:
            return val
    return None


def _enforce_verdict(body: str, event: str, pr: Any = None) -> tuple[str, str]:
    """Programmatically enforce verdict rules based on scorecard scores and safety checks.

    Parses the review body, extracts scores and confidence, and overrides
    the LLM-chosen event if it violates mechanical verdict or CI safety rules.

    Returns:
        Tuple of (possibly_modified_body, enforced_event).
    """
    scores = _parse_scorecard_scores(body)
    confidence = _parse_confidence(body)
    original_event = event.upper()
    enforced_event = original_event
    override_reasons: list[str] = []

    if scores:
        min_score = min(scores)
        avg_score = sum(scores) / len(scores)

        if min_score <= 2 and original_event == "APPROVE":
            enforced_event = "REQUEST_CHANGES"
            override_reasons.append(f"scorecard has category scoring {min_score}/5")

        if avg_score < 3.5 and original_event == "APPROVE":
            enforced_event = "REQUEST_CHANGES"
            override_reasons.append(
                f"average score {avg_score:.1f} is below 3.5 threshold"
            )

    if confidence is not None and confidence <= 3 and original_event == "APPROVE":
        enforced_event = "COMMENT"
        override_reasons.append(
            f"confidence level {confidence}/5 is too low to approve"
        )

    # Programmatic check for dropped platform markers in lockfiles
    if (
        "sys_platform ==" in body
        and "dropped" in body.lower()
        and original_event == "APPROVE"
    ):
        enforced_event = "REQUEST_CHANGES"
        override_reasons.append(
            "detected potential environment marker removal in lockfile diff"
        )

    # Programmatic CI build check and mergeability guardrails.
    #
    # NOTE: The previous implementation called commit.get_combined_status(), which
    # hits the "Get the combined status for a specific reference" endpoint. That
    # endpoint returns 403 "Resource not accessible by integration" for the GitHub
    # App installation (no status-read permission). We now rely on the PR's
    # server-computed `mergeable_state` field, which GitHub already populates with
    # CI check results:
    #   - "dirty"   -> merge conflicts with base branch
    #   - "blocked" -> failing or pending required CI checks
    #   - "behind"  -> head branch is behind base branch
    if pr and original_event == "APPROVE":
        try:
            mergeable_state = getattr(pr, "mergeable_state", "") or ""
            if mergeable_state in ("dirty", "blocked", "behind"):
                enforced_event = "REQUEST_CHANGES"
                if mergeable_state == "dirty":
                    override_reasons.append(
                        "PR has unresolved merge conflicts with base branch"
                    )
                else:
                    override_reasons.append(
                        f"PR is not cleanly mergeable "
                        f"(mergeable_state='{mergeable_state}'; blocked by "
                        "failing/pending CI checks or behind base branch)"
                    )
        except Exception as gate_err:  # noqa: BLE001
            logger.warning("Could not check PR CI/mergeability gating: %s", gate_err)

    if override_reasons and enforced_event != original_event:
        reasons_str = "; ".join(override_reasons)
        body += (
            f"\n\n---\n"
            f"> **Verdict Override:** Agent requested `{original_event}` but "
            f"was overridden to `{enforced_event}` by policy guardrail "
            f"({reasons_str})."
        )
        logger.warning(
            "Verdict override: %s -> %s (%s)",
            original_event,
            enforced_event,
            reasons_str,
        )

    return body, enforced_event


def review(
    ctx: Context,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
) -> str:
    """Submit a formal review on a pull request.

    The verdict is programmatically enforced based on scorecard scores
    parsed from the review body. If the agent's chosen event violates
    the verdict rules, it is overridden before submission.

    Args:
        pr_number: Pull request number.
        body: Review body (Markdown).
        event: Review event type. One of APPROVE, COMMENT, REQUEST_CHANGES.

    Returns:
        A string describing the result.
    """
    repo_name = _get_repo_full_name(ctx)
    target_key = f"{repo_name}#{pr_number}"
    if not _COMMENT_RATE_LIMITER.is_allowed(target_key):
        return (
            f"Error: Review/comment rate limit exceeded for #{pr_number} "
            f"(max 3 comments per minute per thread)."
        )

    gh = _get_gh_from_ctx(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        body, event = _enforce_verdict(body, event, pr)

        # Supersede / dismiss prior bot reviews
        existing_reviews = pr.get_reviews()
        for prev_rv in existing_reviews:
            if (
                prev_rv.user
                and (
                    prev_rv.user.login in (BOT_LOGIN, "hannibal-hub-agents")
                    or prev_rv.user.login.startswith("hannibal-hub-agents")
                )
                and prev_rv.state in ("CHANGES_REQUESTED", "APPROVED")
            ):
                try:
                    prev_rv.dismiss("Superseded by fresh code review on latest commit.")
                    logger.info(
                        "Dismissed prior bot review %s on PR #%d",
                        prev_rv.id,
                        pr_number,
                    )
                except Exception as dismiss_err:  # noqa: BLE001
                    logger.warning(
                        "Could not dismiss prior bot review %s: %s",
                        prev_rv.id,
                        dismiss_err,
                    )

        rv = pr.create_review(body=body, event=event)
        _COMMENT_RATE_LIMITER.record(target_key)
        detail = getattr(rv, "html_url", str(rv))
        return f"Submitted review ({event}): {detail}"
    except Exception as e:  # noqa: BLE001
        return f"Error submitting review: {e}"


# ---------------------------------------------------------------------------
# Utility & Sub-Agent Tools
# ---------------------------------------------------------------------------


def get_current_time(ctx: Context) -> dict[str, str]:
    """Get the current UTC date and time in ISO 8601 format.

    Returns:
        A dictionary containing current_utc_time string.
    """
    return {"current_utc_time": datetime.now(UTC).isoformat()}


# Sub-agent for Google Search grounding without breaking AFC for root tools
search_sub_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMINI_SEARCH_MODEL", "gemini-3.5-flash-lite"),
    instruction="You are a technical search specialist. Search the web for documentation, CVEs, syntax issues, and library details.",
    tools=[google_search],
)

search_tool = AgentTool(search_sub_agent)


# ---------------------------------------------------------------------------
# System instruction for the agent
# ---------------------------------------------------------------------------

# Load templates once at module load time
_PR_TEMPLATE = _load_pr_template()
_CODE_REVIEW_TEMPLATE = _load_code_review_template()

SYSTEM_INSTRUCTION = f"""You are a skilled autonomous GitHub Webhook Agent for the Hannibal Hub ecosystem.

Your reasoning process follows 7 steps:

1. **Understand Intent & Context**: Analyze the incoming event, user sender, PR/issue details, and conversation history.
2. **Autonomous Action Decision & Self-Awareness**: Decide if an action is required, and check for duplication:
   - **DUPLICATE SUPPRESSION RULE**: If you (hannibal-hub-agents[bot]) already submitted a formal review for the PR within the last 120 seconds or for the current head commit SHA, **DO NOT** submit another formal review!
   - When a user requests a code review (`/review`, `@hannibal-hub-agents review`, or PR opened): Call `get_issue(number, include_diff=True)` to inspect the code changes, then invoke `review(pr_number, body=...)` to post a formal code review.
   - **PR Synchronize (`pull_request.synchronize`)**: Call `get_commit_diff(before_sha, head_sha)` to review ONLY the newly pushed commits. Then invoke `review(pr_number, body=..., event="APPROVE")` if all prior feedback is resolved, or `event="REQUEST_CHANGES"` if new issues are found.
   - When a PR description update is requested (`/create`): Call `get_issue(number, include_diff=True)`, format body using the PR template, call `update_issue(number, body=...)`, and invoke `review(pr_number, body=..., event=...)`.
   - When conflict resolution is requested (`/resolve`): Call `update_branch_from_base(pr_number)`. NEVER use `write_file` to attempt resolving git merge conflicts. If `update_branch_from_base` fails, inform the user that a local `git rebase` is required.
   - When a user asks a question or directly mentions @hannibal-hub-agents: Execute the requested operation using appropriate tools.
   - If the event is routine metadata without a command or question, respond in plain text explaining why no tool call is needed.
3. **Grounding Pre-Check**: Before claiming that code, teardown blocks, or unit tests are missing in a PR review:
   - You MUST call `read_file()` or `search_agent()` to inspect the target files first.
   - Never suggest creating a unit test file or adding cleanup logic without first verifying existing tests in tests/ or teardown blocks in the target module.
4. **Validate Tool Parameters**: Verify pr_number, branch names, file_paths, and commit messages before calling tools. Use get_current_time if date/time calculations are needed.
5. **Execute Primitives**: Call read_file, write_file, get_issue, get_commit_diff, update_issue, add_comment, open_pr, update_branch_from_base, merge_pr, review, get_current_time, or search_agent.
6. **Format Results**: Structure reviews, PR descriptions, and responses in Markdown tables, code blocks, and clear sections. Use the code_review_template.md for review output.
7. **Execution Summary**: Summarize completed actions clearly.

Available tools:
  Files API:  read_file, write_file
  Issues API: get_issue, update_issue, add_comment
  Pulls API:  get_commit_diff, open_pr, update_branch_from_base, merge_pr, review
  Utilities:  get_current_time, search_agent (for web search & docs)

Dynamic PR Review Status Transitions:
  - When suggestions/issues found: MUST call review(pr_number, body, event="REQUEST_CHANGES").
  - When all feedback is resolved by a new commit: MUST call review(pr_number, body, event="APPROVE").
  - When responding to general questions: use add_comment(number, body=...) or review(..., event="COMMENT").

---

## Code Review Protocol (MANDATORY)

You are a SENIOR ENGINEER performing code reviews, not a cheerleader. Your job is to catch problems, protect code quality, and provide honest, actionable feedback. Agreeing with everything is a failure mode.

### Review Procedure

When reviewing a PR, you MUST:
1. Call `get_issue(number, include_diff=True)` to fetch the full diff.
2. Analyze every changed file systematically for correctness, security, performance, readability, and test coverage.
3. Structure your review body using the Code Review Template below.
4. Fill in ALL scorecard categories with honest scores and cite specific evidence from the diff.
5. Determine the verdict MECHANICALLY from the scorecard (see Verdict Rules).
6. Call `review(pr_number, body=..., event=VERDICT)` where VERDICT is APPROVE, REQUEST_CHANGES, or COMMENT.

### Verdict Rules (Non-Negotiable)

These rules override your judgment. Apply them mechanically based on your scorecard:
- ANY category scoring 1 (Critical) -> event MUST be REQUEST_CHANGES
- ANY category scoring 2 (Poor) -> event MUST be REQUEST_CHANGES
- Average score below 3.5 -> event MUST be REQUEST_CHANGES
- Your confidence level is 3 or below -> event MUST be COMMENT (never APPROVE when uncertain)
- All categories 3+ AND average >= 3.5 AND confidence >= 4 -> event MAY be APPROVE

### Critical Thinking Requirements

- Finding zero issues is suspicious. If a PR changes more than 10 lines and you have no suggestions, re-read the diff more carefully.
- Every review MUST include at least ONE specific, actionable suggestion — even for excellent code (naming improvements, documentation gaps, test ideas, edge cases).
- Never say code is "rock-solid" or "verified" without citing specific evidence for each claim.
- Do not summarize what the code does back to the author — they already know. Focus on what could go WRONG.
- If the PR is large (>500 lines changed), recommend splitting it and note this in your review.

### Common Issues to Watch For

Always scan for these patterns, which are frequently missed:
- Off-by-one errors in loop boundaries or string slicing
- Missing null/None checks on API responses or dictionary lookups
- Race conditions in async or multi-threaded code
- Environment variables read at import time vs. runtime
- Exception handlers that swallow errors silently (bare except, catch-all without re-raise)
- Hardcoded secrets, API keys, project IDs, or environment-specific values
- Missing input validation on user-provided or external data
- Resource leaks (unclosed files, connections, clients)
- String formatting that breaks on Unicode or special characters
- Missing error handling on network calls, file I/O, or database operations

### Dependabot / Dependency PR Protocol (MANDATORY)

When reviewing Dependabot PRs (`sender: dependabot[bot]` or branch starting with `dependabot/`):
- Focus on **dependency security, version scope, and lockfile integrity**.
- Do NOT perform a human architectural code review — evaluate version bumps and lockfile changes.
- Check if `pyproject.toml` or `package.json` updates match `uv.lock` or `package-lock.json`.
- Watch for **accidental environment marker deletions** (e.g., dropping `sys_platform == 'win32'`) or unexpected modifications to unrelated packages in the lockfile.
- If lockfile changes modify unrelated packages or drop environment markers unexpectedly, you MUST select `REQUEST_CHANGES`.

### Code Review Template

{_CODE_REVIEW_TEMPLATE}

---

### PR Description & Template Evaluation Protocol (MANDATORY)

When generating or updating PR descriptions (`open_pr` or `update_issue`):
1. **Diff Evaluation**: Inspect the git diff and list of changed files.
   - **Dev PR (`dev_pull_request_template.md`)**: Select if changes are strictly limited to `dev/`, `scripts/`, `docs/`, or non-production tooling.
   - **Prod PR (`prod_pull_request_template.md`)**: Select if changes touch `src/`, `rag_service/`, core APIs, or production architecture.
2. **Template Sanitization**: Fill out the appropriate section headers. STRICTLY PROHIBITED: NEVER include template instruction headers (e.g. `# 🤖 Pull Request Description Template` or `## 📋 Title Format`) or placeholder instruction comments in your output. Start directly with the section headers (`## 🗒️ Description` or `## 🍵 The Tea`).

Default fallback layout:
{_PR_TEMPLATE}
"""

# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


# Retry configuration for transient server errors
_MAX_RETRIES = int(os.environ.get("GEMMA_MODEL_MAX_RETRIES", "5"))


class WebhookAgent:
    """ADK-powered agent for processing GitHub webhook events.

    Wraps the ADK Agent and Runner to provide a synchronous interface
    compatible with the existing webhook pipeline.

    Supports automatic model fallback when the primary model is unavailable.
    """

    def __init__(
        self,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._app_name = "hannibal-hub-agents"

        # Session service — keeps per-PR conversation history
        self._session_service = InMemorySessionService()

        # Memory service — in-memory conversation memory
        self._memory_service = InMemoryMemoryService()

        # Track current model chain (TPM Descending)
        self._model_chain = get_model_chain()
        self._chain_index = 0
        self._current_model_name = self._model_chain[self._chain_index]
        self._fallback_triggered = False

        # Ensure API key is resolved and propagated to env vars before model init
        get_active_api_key()

        # Create the ADK agent with all tools
        self._agent = Agent(
            name="webhook_agent",
            model=Gemini(
                model=self._current_model_name,
                client_kwargs={"api_key": get_active_api_key()},
            ),
            instruction=SYSTEM_INSTRUCTION,
            tools=[
                read_file,
                write_file,
                get_issue,
                get_commit_diff,
                update_issue,
                add_comment,
                open_pr,
                update_branch_from_base,
                merge_pr,
                review,
                get_current_time,
                search_tool,
            ],
        )

        # Create the runner
        self._runner = Runner(
            agent=self._agent,
            app_name=self._app_name,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _advance_model_chain(self, error: Exception | None = None) -> str | None:
        """Cascade to next model in TPM descending chain on rate limit or server error."""
        # Mark current model depleted with smart metric-aware cooldown parsing
        _DEPLETED_MODEL_REGISTRY.mark_depleted(self._current_model_name, error=error)

        # Refresh model chain to get available non-depleted models
        self._model_chain = get_model_chain()
        self._chain_index = 0

        # Prioritize picking a different model candidate to prevent self-cascading
        other_models = [m for m in self._model_chain if m != self._current_model_name]
        if other_models:
            next_model = other_models[0]
        elif self._model_chain:
            next_model = self._model_chain[0]
            time.sleep(2.0)
        else:
            return None

        logger.warning(
            "Cascading model chain from %s -> %s",
            self._current_model_name,
            next_model,
        )
        self._current_model_name = next_model
        self._agent.model = Gemini(
            model=next_model,
            client_kwargs={"api_key": get_active_api_key()},
        )
        return next_model

    def _create_fallback_agent(self, error: Exception | None = None) -> None:
        """Switch to fallback model when primary model is unavailable."""
        self._advance_model_chain(error=error)

        # Recreate runner with new agent
        self._runner = Runner(
            agent=self._agent,
            app_name=self._app_name,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _derive_session_id(self, event_data: dict[str, Any]) -> str:
        """Derive a session ID from the event data for conversation continuity.

        Uses repo_full_name + issue/PR number so that follow-up comments
        on the same thread share a session.
        """
        repo = event_data.get("repository", {})
        repo_name = repo.get("full_name", "unknown")
        raw = event_data.get("raw_payload", {})
        issue = raw.get("issue", {})
        pr = raw.get("pull_request", {})
        number = issue.get("number") or pr.get("number")
        if number:
            return f"{repo_name}/{number}"
        return f"{repo_name}/{event_data.get('delivery_id', 'unknown')}"

    def _build_user_message(self, event_data: dict[str, Any]) -> genai_types.Content:
        """Build a user message from the webhook event data."""
        canonical = event_data.get("canonical", "unknown")
        sender = event_data.get("sender", {})
        sender_login = sender.get("login", "unknown")
        raw = event_data.get("raw_payload", {})

        # Build context from the event
        parts: list[str] = [
            f"Canonical Event: {canonical}",
            f"Sender: {sender_login}",
        ]

        # Add event-specific context
        if canonical.startswith("issue_comment."):
            comment = raw.get("comment", {})
            issue = raw.get("issue", {})
            comment_body = (comment.get("body") or "")[:500]
            is_pr = bool(issue.get("pull_request"))
            pr_num = issue.get("number", "unknown")
            parts.append(f"Issue/PR Number: {pr_num}")
            parts.append(f"Thread Type: {'Pull Request' if is_pr else 'Issue'}")
            parts.append(f"Comment: {comment_body}")
            if is_pr:
                parts.append(
                    f"Note: This comment is on Pull Request #{pr_num}. "
                    f"To perform requested actions like code reviews (/review), descriptions (/create), "
                    f"or conflict resolution (/resolve), first call get_issue({pr_num}, include_diff=True) "
                    f"to inspect the PR metadata and code changes."
                )
        elif canonical.startswith("pull_request."):
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"PR Title: {pr.get('title', 'N/A')}")
            parts.append(f"PR Body: {(pr.get('body') or '')[:500]}")
            parts.append(f"PR Head Branch: {(pr.get('head') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Base Branch: {(pr.get('base') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Additions: {pr.get('additions', 'N/A')}")
            parts.append(f"PR Deletions: {pr.get('deletions', 'N/A')}")
            parts.append(f"PR Changed Files: {pr.get('changed_files', 'N/A')}")
        elif canonical.startswith("pull_request_review_comment."):
            comment = raw.get("comment", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review Comment: {(comment.get('body') or '')[:500]}")
        elif canonical.startswith("pull_request_review."):
            review = raw.get("review", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review: {(review.get('body') or '')[:500]}")

        # Include PR diff if available
        if "pr_diff" in raw:
            parts.append(f"\nPR Diff:\n{raw['pr_diff']}")

        text = "\n".join(parts)
        text = _truncate_text_to_token_limit(
            text, max_tokens=get_max_input_tokens(), label="User payload"
        )
        return genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=text)],
        )

    def plan_and_execute(
        self,
        event_data: dict[str, Any],
        gh_client: Github,
        trace_id: str,
    ) -> list[ActionResult]:
        """Process a webhook event through the ADK agent.

        This is the main entry point, called from agent_core.run().

        Args:
            event_data: Normalized webhook event data.
            gh_client: Authenticated GitHub client.
            trace_id: Trace ID for logging.

        Returns:
            List of ActionResult objects.
        """
        repo_full_name = (
            event_data.get("repository", {}).get("full_name")
            if event_data.get("repository")
            else "unknown"
        )

        # Check writeback policy for bot-authored events
        canonical = event_data.get("canonical", "")

        logger.debug(
            "🔍 Checking writeback policy: canonical=%s, dry_run=%s, trace=%s",
            canonical,
            self.dry_run,
            trace_id[-4:],
        )

        if _is_bot_event(event_data):
            sender = event_data.get("sender", {})
            logger.debug(
                "🤖 Bot event detected: sender=%s, canonical=%s",
                sender.get("login", "unknown"),
                canonical,
            )
            logger.info(
                "writeback blocked: bot-originated event '%s' (trace: %s)",
                canonical,
                trace_id[-4:],
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"writeback policy: bot-originated event '{canonical}' blocked",
                )
            ]

        # Check read-only events
        read_only_events: set[str] = {
            "ping",
            "unknown",
        }
        if canonical in read_only_events:
            logger.debug(
                "📖 Read-only event detected: canonical=%s",
                canonical,
            )
            logger.info(
                "writeback policy: event '%s' is read-only (trace: %s)",
                canonical,
                trace_id[-4:],
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"writeback policy: event '{canonical}' is read-only",
                )
            ]

        # Check mutation policy
        allow_auto = os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "0") in (
            "1",
            "true",
            "True",
        )
        if not allow_auto and not self.dry_run:
            logger.debug(
                "⛔ Mutations disabled (ALLOW_AUTOMATED_MUTATIONS=%s)",
                os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "0"),
            )
            logger.info(
                "mutations disabled by policy (trace: %s)",
                trace_id[-4:],
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail="mutations are disabled by policy",
                )
            ]

        if self.dry_run:
            logger.debug("🧪 Dry-run mode enabled")
            logger.info("dry-run mode (trace: %s)", trace_id[-4:])
            return [
                ActionResult(
                    tool="plan",
                    success=True,
                    detail="dry-run: would process event through ADK agent",
                )
            ]

        logger.debug(
            "✅ All policy checks passed, building session context (trace: %s)",
            trace_id[-4:],
        )

        # Derive session and user IDs
        session_id = self._derive_session_id(event_data)
        sender = event_data.get("sender") or {}
        sender_login = sender.get("login", "")
        user_id = sender_login or "anonymous"

        logger.debug(
            "👤 Session context: session_id=%s, user_id=%s",
            session_id,
            user_id,
        )

        # Build the user message
        user_message = self._build_user_message(event_data)
        logger.debug(
            "📝 Built user message for agent (length: %d chars)",
            len(user_message.parts[0].text) if user_message.parts else 0,
        )

        # Select model tier dynamically for this event
        selected_model = _select_model_for_event(event_data)
        if self._current_model_name != selected_model:
            logger.info(
                "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
                selected_model,
                canonical,
                trace_id[-4:],
            )
            self._current_model_name = selected_model
            self._agent.model = Gemini(
                model=selected_model,
                client_kwargs={"api_key": get_active_api_key()},
            )

        # Run the agent asynchronously with retry and fallback support
        results: list[ActionResult] = []
        emitted_texts: list[str] = []

        async def _execute_agent():
            # Apply dynamic sliding-window rate limiting (RPM/TPM aware per tier)
            msg_text = user_message.parts[0].text if user_message.parts else ""
            est_tokens = len(msg_text) // 4 + 500
            await rpm_waiter.check_and_wait(
                model=self._current_model_name,
                estimated_tokens=est_tokens,
            )

            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                # Handle token recording if usage metadata is available
                if hasattr(event, "usage_metadata") and event.usage_metadata:
                    total_tok = getattr(
                        event.usage_metadata, "total_token_count", 0
                    ) or getattr(event.usage_metadata, "total_tokens", 0)
                    if total_tok > 0:
                        await rpm_waiter.record_actual_tokens(
                            model=self._current_model_name,
                            actual_tokens=total_tok,
                        )

                # Handle function response events — these are tool results from ADK
                if hasattr(event, "get_function_responses"):
                    responses = event.get_function_responses()
                    if responses:
                        logger.debug(
                            "🔧 Received %d tool responses from ADK",
                            len(responses),
                        )
                        for response in responses:
                            results.append(
                                ActionResult(
                                    tool=response.name,
                                    success=True,
                                    detail=f"tool executed: {response.response}",
                                )
                            )

                # Handle text responses — log the agent's reasoning
                if (
                    event.content
                    and event.content.parts
                    and any(hasattr(p, "text") and p.text for p in event.content.parts)
                ):
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            emitted_texts.append(part.text)
                            logger.debug(
                                "💭 Agent response received (trace: %s): %s",
                                trace_id[-4:],
                                part.text[:200],
                            )
                            logger.info(
                                "🧠 Agent response: %s (trace: %s)",
                                part.text[:200],
                                trace_id[-4:],
                            )

        async def _run():
            nonlocal results
            last_error = None

            # Try with retry and optional fallback model
            for attempt in range(_MAX_RETRIES):
                try:
                    # Ensure session exists before invoking the runner.
                    # In the installed ADK version, InMemorySessionService only
                    # exposes async helpers, so we must await them here.
                    session = await self._session_service.get_session(
                        app_name=self._app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if session is None:
                        await self._session_service.create_session(
                            app_name=self._app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        # Re-fetch the session after creation
                        session = await self._session_service.get_session(
                            app_name=self._app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Created new ADK session %s for user %s",
                            session_id,
                            user_id,
                        )

                    # Deduplication check: if a review was submitted < 30s ago, inject notice
                    if session and session.state:
                        last_review_ts = session.state.get("last_review_timestamp", 0)
                        now = time.time()
                        time_since_review = now - last_review_ts
                        if time_since_review < 30.0:
                            notice = (
                                f"\n\n[⚠️ SYSTEM NOTICE: You submitted a formal PR review {time_since_review:.1f} seconds ago. "
                                "Do NOT call review() again unless explicitly requested by a new /review command.]"
                            )
                            if user_message.parts and hasattr(
                                user_message.parts[0], "text"
                            ):
                                user_message.parts[0].text += notice

                        previous_critique = session.state.get(
                            "last_review_critique", ""
                        )
                        if previous_critique:
                            critique_notice = (
                                f"\n\n[YOUR PREVIOUS REVIEW CRITIQUE]:\n{previous_critique}\n"
                                "Verify line-by-line which specific items were resolved by the new commit."
                            )
                            if user_message.parts and hasattr(
                                user_message.parts[0], "text"
                            ):
                                user_message.parts[0].text += critique_notice

                    # Set user_state values - they get merged into session.state by InMemorySessionService
                    # This is needed because session copies are returned and our direct mutations wouldn't persist
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["gh_client"] = gh_client
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["repo_full_name"] = repo_full_name
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["sender"] = user_id

                    # Execute the ADK runner with current model
                    await _execute_agent()
                    return  # Success - exit the retry loop

                except Exception as e:
                    last_error = e
                    if _is_transient_error(e) and attempt < _MAX_RETRIES - 1:
                        self._advance_model_chain(error=e)
                        logger.warning(
                            "Transient error on attempt %d/%d (trace: %s): %s. Active model is now: %s",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            e,
                            self._current_model_name,
                        )
                        continue
                    logger.debug(
                        "Non-transient error or exhausted retries: raising exception (trace: %s)",
                        trace_id[-4:],
                    )
                    logger.exception(
                        "ADK agent run failed (trace: %s)",
                        trace_id[-4:],
                    )
                    results.append(
                        ActionResult(
                            tool="plan",
                            success=False,
                            detail=f"ADK agent error: {e}",
                        )
                    )
                    return

            # If we exhausted retries, add error result
            if last_error and _is_transient_error(last_error):
                logger.debug(
                    "All retry attempts exhausted (trace: %s): retrying model was unavailable",
                    trace_id[-4:],
                )
                logger.error(
                    "Model unavailable after %d retries (trace: %s)",
                    _MAX_RETRIES,
                    trace_id[-4:],
                )
                results.append(
                    ActionResult(
                        tool="plan",
                        success=False,
                        detail=f"Model unavailable after {_MAX_RETRIES} retries: {last_error}",
                    )
                )

        # Run the ADK coroutine on the persistent background loop to avoid
        # "Event loop is closed" issues when the process receives signals or
        # when httpx/anyio transports attempt to close transports on a loop
        # that has been shut down. This schedules the coroutine and waits
        # for completion synchronously.
        run_in_bg_loop(_run())

        # Programmatic review fallback: if no review tool was called during a PR review event,
        # but text critique/scorecard was produced, post the review programmatically.
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})
        comment_body = (
            (raw.get("comment", {}) or {}).get("body", "")
            if isinstance(raw, dict)
            else ""
        )
        is_pr_review_event = (
            canonical.startswith(("pull_request.", "pull_request_review"))
            or "/review" in comment_body
        )
        has_review_action = any(r.tool == "review" for r in results)

        if is_pr_review_event and not has_review_action and emitted_texts:
            full_text = "\n\n".join(emitted_texts)
            if (
                "Scorecard" in full_text
                or "| Category |" in full_text
                or "Verdict:" in full_text
            ):
                pr_number = None
                if isinstance(raw, dict):
                    pr_number = (raw.get("pull_request") or {}).get("number") or (
                        raw.get("issue") or {}
                    ).get("number")

                if pr_number:
                    try:
                        repo = gh_client.get_repo(repo_full_name)
                        pr = repo.get_pull(pr_number)
                        body, enforced_event = _enforce_verdict(
                            full_text, "COMMENT", pr
                        )
                        rv = pr.create_review(body=body, event=enforced_event)
                        detail = getattr(rv, "html_url", str(rv))
                        results.append(
                            ActionResult(
                                tool="review",
                                success=True,
                                detail=f"Programmatic fallback review ({enforced_event}): {detail}",
                            )
                        )
                        logger.info(
                            "Programmatic fallback review submitted for PR #%d (%s)",
                            pr_number,
                            enforced_event,
                        )
                    except Exception as fallback_err:  # noqa: BLE001
                        logger.warning(
                            "Programmatic fallback review submission failed: %s",
                            fallback_err,
                        )

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id[-4:],
            )

        return results
