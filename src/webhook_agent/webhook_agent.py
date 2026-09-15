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
from collections.abc import Coroutine
from concurrent.futures import CancelledError, Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github import Github
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Workflow
from google.genai import types as genai_types
from google.genai.errors import ServerError as GenAIServerError

from webhook_agent.logic.model_factory import RateLimitedGemini, get_adk_model
from webhook_agent.logic.plugins import (
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)
from webhook_agent.logic.rate_limiter import (
    _resolve_tier,
    extract_rate_limit_details,
    get_active_api_key,
    rpm_waiter,
)

from .audit_schema import AuditVerdict
from .bot_identity import _is_bot_event
from .callbacks import (
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from .comment_poster import build_github_review_comments
from .formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    extract_json_payload,
    normalize_code_review_dict,
    normalize_sync_review_dict,
    parse_text_review_to_dict,
    render_code_review_markdown,
    render_sync_review_markdown,
)
from .logic.diff_filter import filter_review_diff
from .memory_service import InMemoryMemoryService
from .sanitizer_plugin import PromptSanitizerPlugin
from .schemas import CodeReviewResponse, IssueItem, SyncReviewResponse
from .tools.diff_tools import get_pr_diff_file_map_tool, verify_line_reference_tool
from .tools.resolve_conflicts import resolve_merge_conflicts
from .tools.search_tool import google_search_grounding_tool
from .webhook_types import ActionResult


def calculate_verdict(
    scores: dict[str, int] | None = None,
    confidence: int = 5,
    has_critical: bool = False,
) -> str:
    """Calculates PR review verdict cleanly.

    Rules:
    - If has_critical or (scores and any(s <= 2 for s in scores.values())): REQUEST_CHANGES
    - Otherwise: APPROVE
    """
    if has_critical:
        return "REQUEST_CHANGES"
    if scores:
        if any(s <= 2 for s in scores.values()):
            return "REQUEST_CHANGES"
        avg_score = sum(scores.values()) / len(scores)
        if avg_score < 3.5:
            return "REQUEST_CHANGES"
    return "APPROVE"


__all__ = [
    "RateLimitedGemini",
    "WebhookAgent",
    "calculate_verdict",
    "get_active_model",
    "get_adk_model",
]

# Persistent background event loop used to run ADK coroutines safely from
# synchronous callers. Using a single long-lived loop prevents repeatedly
# creating and closing event loops (which caused "Event loop is closed"
# errors when background transports attempted to schedule callbacks during
# loop shutdown). We run the loop in a daemon thread and schedule coroutines
# onto it via asyncio.run_coroutine_threadsafe.
_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_LOOP_THREAD: threading.Thread | None = None
_GENAI_CLIENT: Any = None


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


def run_in_bg_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """Schedule coroutine on the background loop and wait for result."""
    loop = _ensure_bg_loop()
    future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        # Wait for result; use a 600s timeout to allow complex multi-step reasoning
        # and rate-limiter pauses without prematurely failing the delivery.
        return future.result(timeout=600)
    except CancelledError:
        raise
    except Exception:
        # Re-raise after logging to make debugging easier in logs
        logger.exception("Error running coroutine in background loop")
        raise


async def _create_genai_client_async(api_key: str) -> Any:
    """Create a google.genai Client on the background loop thread."""
    from google.genai import Client

    # Construct the client synchronously on the background loop to bind any
    # async transports to that loop's lifecycle.
    return Client(api_key=api_key)


def get_shared_genai_client() -> Any:
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


logger = logging.getLogger("webhook_agent.agent")


def get_active_model(event_data: dict[str, Any] | None = None) -> str:
    """Return default active model name for the agent, using dynamic model routing and depletion registry."""
    if event_data is not None:
        return _select_model_for_event(event_data)
    chain = get_model_chain()
    if chain:
        return chain[0]
    active_tier = _resolve_tier()
    default_primary = "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.8-flash"
    return os.getenv("GEMMA_MODEL", default_primary)


def _get_model_tpm_limit(model: str = "default", tier: str | None = None) -> int:
    """Reads TPM limit for model and tier from gemini_models.json."""
    active_tier = tier or _resolve_tier()
    target_model = model if model and model != "default" else get_active_model()
    try:
        import json
        from pathlib import Path

        candidates = [
            Path(__file__).resolve().parents[1] / "assets" / "registries" / "gemini_models.json",
            Path(__file__).resolve().parents[2] / "assets" / "registries" / "gemini_models.json",
        ]
        registry_path = next((p for p in candidates if p.exists()), candidates[1])
        if registry_path.exists():
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            full_key = (
                target_model if target_model.startswith("models/") else f"models/{target_model}"
            )
            for m in data.get("models", []):
                if isinstance(m, dict) and m.get("name") in (target_model, full_key):
                    rate_limits = m.get("rate_limits", {})
                    tier_data = rate_limits.get(active_tier, {})
                    if isinstance(tier_data, dict):
                        tpm_val = tier_data.get("tpm", 0)
                        if isinstance(tpm_val, (int, float)) and tpm_val > 0:
                            return int(tpm_val)
    except Exception:
        pass
    return 15000 if active_tier == "free" else 100000


def _count_tokens_exact(text: str, model: str | None = None) -> int:
    """Uses Google GenAI free count_tokens API method with proper active key, model, and tier."""
    if not text:
        return 0
    active_key = get_active_api_key()
    if not active_key:
        return len(text) // 4

    target_model = model if model and model != "default" else get_active_model()
    try:
        from google import genai

        client = genai.Client(api_key=active_key)
        resp = client.models.count_tokens(model=target_model, contents=text)
        if resp and resp.total_tokens:
            return int(resp.total_tokens)
    except Exception:
        pass
    return len(text) // 4


def _truncate_input_for_tier(
    text: str,
    model: str = "default",
    tier: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Chunk/truncate text input to remain strictly below TPM rate limits on Free Tier."""
    if not text:
        return text

    active_tier = tier or _resolve_tier()
    if active_tier != "free":
        return text

    target_model = model if model and model != "default" else get_active_model()
    tpm_limit = _get_model_tpm_limit(target_model, active_tier)
    target_tokens = max_tokens or min(15000, max(1000, int(tpm_limit * 0.85)))

    current_tokens = _count_tokens_exact(text, model=target_model)
    if current_tokens <= target_tokens:
        return text

    max_chars = target_tokens * 4
    truncated_msg = (
        f"\n\n[Content truncated to {target_tokens} tokens for Free Tier TPM limit "
        f"({current_tokens} tokens -> {target_tokens} tokens)]"
    )
    return text[: max_chars - len(truncated_msg)] + truncated_msg


# RateLimitedGemini is imported from webhook_agent.logic.model_factory above


# ---------------------------------------------------------------------------
# Template Loading Utility
# ---------------------------------------------------------------------------


def _load_prompt_template(filename: str) -> str:
    """Load prompt template from local templates directory."""
    template_path = Path(__file__).parent / "templates" / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""


def _load_template(filename: str) -> str:
    """Load a template file from the templates directory (backward compatibility)."""
    return _load_prompt_template(filename)


def _sanitize_pr_body(body: str) -> str:
    """Programmatically strip raw template instruction headers and placeholders from PR bodies,
    and convert auto-closing issue keywords (Closes #X) to tracking references (Addresses #X).
    """
    if not body:
        return body

    # Convert auto-closing keywords to tracking references to prevent premature issue closure
    body = re.sub(
        r"\b(Closes|Fixes|Resolves)\s+#(\d+)\b",
        r"Addresses #\2",
        body,
        flags=re.IGNORECASE,
    )

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


def _fetch_repo_pr_template(gh: Any, repo_name: str, changed_files: list[str] | None = None) -> str:
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
        except Exception as exc:
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
            except Exception as exc:
                logger.debug("Could not fetch remote template path %s: %s", path, exc)
                continue
    except Exception as exc:
        logger.debug("Could not fetch remote PR template for %s: %s", repo_name, exc)

    return _load_pr_template()


def _load_pr_template() -> str:
    """Load the PR description template from the templates directory."""
    return _load_template("pr_template.md")


def _load_code_review_template() -> str:
    """Load the code review template from the templates directory."""
    return _load_template("code_review_template.md")


def _load_sync_review_template() -> str:
    """Load the synchronization review template from the templates directory."""
    return _load_template("sync_review_template.md")


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


def count_tokens_exact(contents: str | list[Any], model_name: str | None = None) -> int | None:
    """Count input tokens using Google GenAI SDK's client.models.count_tokens()."""
    target_model = model_name or get_active_model()
    try:
        client = get_shared_genai_client()
        if client is None:
            return None
        res = client.models.count_tokens(
            model=target_model,
            contents=contents if isinstance(contents, list) else [contents],
        )
        return getattr(res, "total_tokens", None)
    except Exception as exc:
        logger.debug("count_tokens API call skipped/unavailable: %s", exc)
        return None


def _truncate_text_to_token_limit(
    text: str,
    max_tokens: int | None = None,
    model_name: str | None = None,
    label: str = "Input",
) -> str:
    """Preserve full text payload without truncation."""
    return text


# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


# Retry configuration for transient server errors
_MAX_RETRIES = int(os.environ.get("GEMMA_MODEL_MAX_RETRIES", "5"))


# ---------------------------------------------------------------------------
# Process-Wide Model Depletion Registry
# ---------------------------------------------------------------------------


class DepletedModelRegistry:
    """Tracks models that have hit 429 quota exhaustion to bypass them process-wide across events."""

    def __init__(self, default_cooldown: float = 3600.0) -> None:
        self.default_cooldown = default_cooldown
        self._depleted: dict[str, tuple[float, float]] = {}

    def _norm(self, name: str) -> str:
        return name.replace("models/", "").strip().lower()

    def mark_depleted(self, model_name: str, error: Exception | None = None) -> None:
        norm_name = self._norm(model_name)
        cooldown = self.default_cooldown
        metric_type = "DEFAULT (1h)"

        if error is not None:
            err_str = str(error).lower()
            if "perday" in err_str or "dayperproject" in err_str:
                cooldown = 86400.0
                metric_type = "RPD (24h)"
            elif (
                "perminute" in err_str
                or "minuteperproject" in err_str
                or "tokensperminute" in err_str
                or "429" in err_str
            ):
                cooldown = 60.0
                metric_type = "RPM/TPM (60s)"
            elif "503" in err_str or "unavailable" in err_str or "high demand" in err_str:
                cooldown = 120.0
                metric_type = "503 HIGH DEMAND (120s)"

        self._depleted[norm_name] = (time.time(), cooldown)
        logger.warning(
            "Model '%s' marked DEPLETED [%s] across process",
            norm_name,
            metric_type,
        )

    def is_depleted(self, model_name: str) -> bool:
        norm_name = self._norm(model_name)
        if norm_name not in self._depleted:
            return False
        timestamp, cooldown = self._depleted[norm_name]
        if time.time() - timestamp > cooldown:
            del self._depleted[norm_name]
            logger.info(
                "Model '%s' depletion cooldown expired (%ds), restored to pool",
                norm_name,
                int(cooldown),
            )
            return False
        return True

    def filter_chain(self, chain: list[str]) -> list[str]:
        return [m for m in chain if not self.is_depleted(m)]


_DEPLETED_MODEL_REGISTRY: Any
try:
    from webhook_agent.logic.firestore_registry import (
        firestore_depleted_registry as _DEPLETED_MODEL_REGISTRY,
    )
except ImportError:
    _DEPLETED_MODEL_REGISTRY = DepletedModelRegistry(default_cooldown=3600.0)


def get_model_chain() -> list[str]:
    """Build ordered list of fallback models sorted by capacity and tier.

    Filters out models currently marked as depleted in _DEPLETED_MODEL_REGISTRY.

    Free Tier Chain:
        1. gemini-3.5-flash-lite (500 RPD / 250k TPM)
        2. gemini-3.1-flash-lite (500 RPD / 250k TPM)
        3. gemma-4-31b-it (14,400 RPD / 16k TPM)
        4. gemma-4-26b-a4b-it (14,400 RPD / 16k TPM)

    Paid Tier Chain:
        1. gemini-3.8-flash (10,000 RPD / 2M TPM)
        2. gemini-3.7-flash (10,000 RPD / 2M TPM)
        3. gemini-3.6-flash (10,000 RPD / 2M TPM)
        4. gemini-3.5-flash-lite (150,000 RPD / 4M TPM)
        5. gemini-3.1-flash-lite (150,000 RPD / 4M TPM)
    """
    active_tier = _resolve_tier()
    if active_tier == "paid":
        default_primary = "gemini-3.8-flash"
        default_chain = [
            default_primary,
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
        ]
    else:
        default_primary = "gemini-3.5-flash-lite"
        default_chain = [
            default_primary,
            "gemini-3.1-flash-lite",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        ]

    primary = os.environ.get("GEMMA_MODEL", default_primary)
    chain = [primary] + [m for m in default_chain if m != primary]
    deduped = list(dict.fromkeys(chain))
    available = _DEPLETED_MODEL_REGISTRY.filter_chain(deduped)
    final_chain = available if available else deduped
    logger.debug("Resolved %s model chain: %s", active_tier, final_chain)
    return final_chain


def _select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type, active tier, and content commands.

    On Free Tier, defaults primary to gemini-3.5-flash-lite (500 RPD) to protect gemini-3.8-flash (20 RPD).
    Routes heavy workloads (pull_request.opened, slash commands, @mentions)
    to the primary model, and routine lifecycle events to the lightweight model.
    """
    active_tier = _resolve_tier()
    default_primary = "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.8-flash"
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
        or "503" in err_str
        or "unavailable" in err_str
        or "high demand" in err_str
        or "resource_exhausted" in err_str
        or "resourceexhausted" in err_type
        or "clienterror" in err_type
        or "servererror" in err_type
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
    except Exception as e:
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
        except Exception:
            sb = repo.get_branch(base)
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sb.commit.sha)
            branch_created = True

        try:
            repo.create_file(file_path, message, content, branch=branch)
        except Exception:
            existing = repo.get_contents(file_path, ref=branch)
            sha = existing[0].sha if isinstance(existing, list) else existing.sha
            repo.update_file(file_path, message, content, sha, branch=branch)
        status = f"Committed '{file_path}' to {branch}"
        if branch_created:
            status += f" (branch created from {base})"
        return status
    except Exception as e:
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
        except Exception:
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
                raw_diff = "\n".join(diff_lines) if diff_lines else "No files changed."
                if raw_diff and raw_diff != "No files changed.":
                    filtered_res = filter_review_diff(raw_diff)
                    diff_text = filtered_res.filtered_diff or raw_diff
                else:
                    diff_text = raw_diff
                parts.append(f"\nDiff:\n{diff_text}")

        else:
            parts.append("Type: Issue")
            body_preview = (issue.body or "")[:500]
            if body_preview:
                parts.append(f"Body: {body_preview}")

        return "\n".join(parts)
    except Exception as e:
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

        # Safeguard: If head_sha is a merge commit from base branch, do not pull in full base compare
        head_commit = repo.get_commit(head_sha)
        parents = getattr(head_commit, "parents", []) or []
        if len(parents) >= 2:
            commit_obj = getattr(head_commit, "commit", None)
            msg = (getattr(commit_obj, "message", "") or "").strip()
            first_line = msg.splitlines()[0] if msg else ""
            if any(
                pat in first_line.lower()
                for pat in (
                    "merge branch",
                    "merge remote-tracking",
                    "merge https://github.com/",
                    "merge commit",
                    "into ",
                )
            ):
                logger.info(
                    "get_commit_diff: commit %s is a branch update merge commit (%s)",
                    head_sha[:7],
                    first_line,
                )
                return (
                    f"Commit {head_sha[:7]} is a branch update merge commit ({first_line}). "
                    f"No new PR-specific code changes were introduced."
                )

        comparison = repo.compare(base_sha, head_sha)
        diff_lines = [f"Incremental Diff ({base_sha[:7]}..{head_sha[:7]}):\n"]
        for f in comparison.files:
            diff_lines.append(
                f"File: {f.filename} ({f.status})\nPatch:\n{f.patch or 'No patch available.'}\n{'-' * 40}"
            )
        raw_diff = "\n".join(diff_lines)
        filtered_res = filter_review_diff(raw_diff)
        return filtered_res.filtered_diff or raw_diff
    except Exception as e:
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

        return f"#{number}: " + "; ".join(actions) if actions else f"#{number}: no changes"
    except Exception as e:
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
    # Programmatic Guardrail: Block duplicate add_comment if formal review() was already submitted in this same execution turn
    session_state = getattr(ctx, "state", None)
    if (isinstance(session_state, dict) and session_state.get("review_submitted_in_this_turn")) or (
        "Successfully audited Pull Request" in body
        or "Skipped: Formal code review" in body
        or "submitted a formal code review report" in body
    ):
        logger.warning(
            "Programmatic Guardrail: Blocked duplicate add_comment() for #%d (formal review status message)",
            issue_number,
        )
        return f"Skipped: Formal code review report already submitted for #{issue_number} in this turn."

    # Programmatic Guardrail: Redirect code review reports erroneously sent to add_comment to review()
    cleaned_b = body.strip()
    if (
        "executive_summary" in body
        or "critical_issues" in body
        or "resolutions" in body
        or "Code Review Report" in body
        or "Audit Report" in body
        or "| Category" in body
        or "**Scorecard**" in body
        or "## 4. Verdict Determination" in body
        or (cleaned_b.startswith("{") and cleaned_b.endswith("}"))
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
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
        return (
            f"Error updating PR #{pr_number} branch: {e}. "
            f"If there are complex merge conflicts, notify the user that manual local rebase is required."
        )


def resolve_pr_conflicts(ctx: Context, pr_number: int) -> str:
    """Surgically resolve git merge conflicts in a pull request using an isolated Git worktree.

    Uses an ephemeral Git Worktree, Gemini generative code block synthesis,
    ruff checking, and pytest verification before pushing. Call this tool
    when a user comments `/resolve` or asks to resolve merge conflicts on a PR.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the conflict resolution status and files modified.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        genai_client = get_shared_genai_client()
        active_model = ctx.state.get("active_model") or get_active_model()
        res = resolve_merge_conflicts(
            pr_number=pr_number,
            head_branch=pr.head.ref,
            base_branch=pr.base.ref,
            genai_client=genai_client,
            model_name=active_model,
        )
        if res.get("success"):
            detail = res.get("detail", "")
            return (
                f"Successfully resolved merge conflicts on PR #{pr_number} "
                f"({pr.head.ref} -> {pr.base.ref}): {detail}"
            )
        return f"Could not resolve merge conflicts on PR #{pr_number}: {res.get('error')}"
    except Exception as e:
        return f"Error resolving merge conflicts on PR #{pr_number}: {e}"


def auto_fix_pr_review_feedback(ctx: Context, pr_number: int) -> str:
    """Autonomously resolve code review feedback for a PR in an isolated Git Worktree.

    Checks policy rules, parses requested changes, applies fixes, verifies tests,
    and commits/pushes the changes to origin. Call this tool when a user comments `/fix`,
    `/auto`, or `/fix-it` on a pull request.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the resolution status.
    """
    from webhook_agent.tools.auto_fix_feedback import auto_fix_pr_feedback

    return auto_fix_pr_feedback(ctx=ctx, pr_number=pr_number)


def mark_ready_for_review(ctx: Context, pr_number: int) -> str:
    """Mark a draft pull request as ready for review.

    Args:
        pr_number: Pull request number to mark ready for review.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        if not getattr(pr, "draft", False):
            return f"PR #{pr_number} is already ready for review (not a draft)."

        success = pr.mark_ready_for_review()
        if success is False:
            return f"Failed to mark PR #{pr_number} ready for review."
        return f"Successfully marked PR #{pr_number} as ready for review."
    except Exception as e:
        return f"Error marking PR #{pr_number} ready for review: {e}"


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

        # Safety Check 0: Draft PR check
        if getattr(pr, "draft", False):
            return (
                f"Error: Cannot merge PR #{pr_number} because it is currently a draft. "
                "Call mark_ready_for_review first or mark it ready for review on GitHub."
            )

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
                user for user, state in latest_reviews.items() if state == "CHANGES_REQUESTED"
            ]
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Active CHANGES_REQUESTED reviews from: {', '.join(blocking)}."
            )

        res = pr.merge(merge_method=merge_method)
        return f"Merged: {res}"
    except Exception as e:
        return f"Error merging PR: {e}"


def _parse_scorecard_scores(body: str) -> list[int]:
    """Extract individual category scores from a review body's scorecard.

    Supports both Callout list format ('* **Category:** N/5') and table format ('| **Category** | N |').
    Returns a list of parsed integer scores, or empty list if none found.
    """
    import re

    scores: list[int] = []
    # Match callout list format: * **Code Correctness:** 5/5
    for match in re.finditer(r"\*\s*\*\*[^*]+\*\*\s*:\s*(\d)(?:/5)?", body):
        score = int(match.group(1))
        if 1 <= score <= 5:
            scores.append(score)

    if not scores:
        # Fallback to table format: | **Code Correctness** | 5 |
        for match in re.finditer(r"\|\s*\*\*[^*]+\*\*\s*\|\s*(\d)\s*\|", body):
            score = int(match.group(1))
            if 1 <= score <= 5:
                scores.append(score)
    return scores


def _parse_confidence(body: str) -> int | None:
    """Extract the confidence self-assessment score from a review body.

    Looks for 'Confidence:' followed by a number 1-5 or N/5.
    Returns the score or None if not found.
    """
    import re

    match = re.search(r"\*\*(?:My\s+)?Confidence:\*\*\s*(\d)", body)
    if match:
        val = int(match.group(1))
        if 1 <= val <= 5:
            return val
    return None


def _enforce_verdict(
    body: str, event: str, pr: Any = None
) -> tuple[str, str, list[dict[str, Any]]]:
    """Programmatically enforce verdict rules based on structured JSON or Markdown text.

    Always parses and normalizes review output into strict Pydantic models (CodeReviewResponse
    or SyncReviewResponse), renders clean Markdown using render_code_review_markdown or
    render_sync_review_markdown, and generates native GitHub inline review comments with
    ```suggestion blocks for diff-anchored issues.

    Safety Invariant: A safety guardrail must ONLY downgrade verdicts (APPROVE -> REQUEST_CHANGES
    or APPROVE -> COMMENT), and must NEVER mechanically upgrade an intended REQUEST_CHANGES to APPROVE!
    """
    import json

    cleaned_body = body.strip()
    req_event = (event or "").strip().upper()
    is_caller_request_changes = req_event == "REQUEST_CHANGES"
    is_body_request_changes = (req_event != "APPROVE") and bool(
        re.search(
            r"##\s*(?:🛡️|⚡)?\s*Code Review(?:\s*Update)?:\s*`?REQUEST_CHANGES`?",
            body,
            re.IGNORECASE,
        )
    )
    is_intended_request_changes = is_caller_request_changes or is_body_request_changes

    # Step 1: Attempt robust JSON payload extraction, quote repair, and object salvage
    extracted_data = extract_json_payload(body)
    data_candidates: list[dict[str, Any]] = [extracted_data] if extracted_data else []

    if not data_candidates:
        # Step 2: Fallback candidate scanning
        json_candidates: list[str] = []
        if cleaned_body.startswith("```"):
            stripped_cb = re.sub(r"^```[a-z]*\n?", "", cleaned_body)
            stripped_cb = re.sub(r"\n?```$", "", stripped_cb).strip()
            json_candidates.append(stripped_cb)

        if cleaned_body.startswith("{") and cleaned_body.endswith("}"):
            json_candidates.append(cleaned_body)

        for cb_match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", body):
            json_candidates.append(cb_match.group(1))

        for json_obj_match in re.finditer(
            r"(\{[\s\S]*?\"(?:executive_summary|resolutions|critical_issues|minor_suggestions)\"[\s\S]*?\})",
            body,
        ):
            json_candidates.append(json_obj_match.group(1))

        for cand in json_candidates:
            try:
                cand_data = json.loads(cand)
                if isinstance(cand_data, dict):
                    data_candidates.append(cand_data)
            except Exception:
                continue

    has_prior_reviews = True
    diff_text = ""
    if pr is not None:
        try:
            reviews = list(pr.get_reviews())
            bot_reviews = []
            for r in reviews:
                u = getattr(r, "user", None)
                login = (getattr(u, "login", "") or "").lower() if u else ""
                if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                    bot_reviews.append(r)
            has_prior_reviews = bool(bot_reviews)
        except Exception:
            has_prior_reviews = True
            bot_reviews = []

        review_budget: int | None = None
        existing_comments_data: list[dict[str, Any]] = []
        try:
            files = pr.get_files()
            diff_lines: list[str] = []
            for f in files:
                patch = getattr(f, "patch", "") or ""
                diff_lines.append(f"+++ b/{f.filename}\n{patch}")
            raw_diff = "\n".join(diff_lines)
            filtered_res = filter_review_diff(raw_diff)
            diff_text = filtered_res.filtered_diff or raw_diff
            review_budget = filtered_res.budget
        except Exception as diff_err:
            logger.debug("Could not fetch PR diff text in _enforce_verdict: %s", diff_err)

        try:
            for c in pr.get_review_comments():
                existing_comments_data.append(
                    {
                        "path": getattr(c, "path", ""),
                        "line": getattr(c, "line", None) or getattr(c, "original_line", None),
                        "body": getattr(c, "body", ""),
                    }
                )
        except Exception as comm_err:
            logger.debug("Could not fetch existing PR review comments: %s", comm_err)
    else:
        review_budget = None
        existing_comments_data = []

    for data in data_candidates:
        try:
            if isinstance(data, dict):
                if is_intended_request_changes and not data.get("verdict"):
                    data["verdict"] = "REQUEST_CHANGES"

                if "resolutions" in data or ("summary" in data and "executive_summary" not in data):
                    normalized_sync = normalize_sync_review_dict(data)
                    sync_obj = SyncReviewResponse.model_validate(normalized_sync)

                    # Programmatic hallucination guard: clear resolutions if no prior review
                    # had actionable CHANGES_REQUESTED items
                    if pr is not None and sync_obj.resolutions:
                        prior_had_changes = any(
                            getattr(r, "state", "") == "CHANGES_REQUESTED" for r in bot_reviews
                        )
                        if not prior_had_changes:
                            logger.warning(
                                "Resolution hallucination guard: Cleared %d fabricated resolutions (no prior CHANGES_REQUESTED reviews exist)",
                                len(sync_obj.resolutions),
                            )
                            sync_obj.resolutions = []

                    enforced_verdict = calculate_sync_verdict(sync_obj)
                    if is_intended_request_changes and enforced_verdict == "APPROVE":
                        logger.warning(
                            "Safety Guardrail: Prevented mechanical upgrade of sync REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
                        )
                        enforced_verdict = "REQUEST_CHANGES"
                    rendered_body = render_sync_review_markdown(
                        sync_obj, enforced_verdict, has_prior_reviews=has_prior_reviews
                    )
                    inline_comments: list[dict[str, Any]] = []
                    if diff_text:
                        sync_issues = list(sync_obj.critical_issues) + list(
                            sync_obj.minor_suggestions
                        )
                        inline_comments, _ = build_github_review_comments(
                            sync_issues,
                            diff_text,
                            existing_comments=existing_comments_data,
                            max_comments=review_budget,
                        )
                    return rendered_body, enforced_verdict, inline_comments
                elif (
                    "executive_summary" in data
                    or "critical_issues" in data
                    or "minor_suggestions" in data
                ):
                    normalized_data = normalize_code_review_dict(data)
                    cr_obj = CodeReviewResponse.model_validate(normalized_data)
                    enforced_verdict = calculate_strict_verdict(cr_obj)
                    if is_intended_request_changes and enforced_verdict == "APPROVE":
                        logger.warning(
                            "Safety Guardrail: Prevented mechanical upgrade of REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
                        )
                        enforced_verdict = "REQUEST_CHANGES"
                        if not cr_obj.critical_issues:
                            crit_desc = (
                                cr_obj.risks_and_edge_cases[0].risk
                                if cr_obj.risks_and_edge_cases
                                else cr_obj.executive_summary
                            )
                            crit_fix = (
                                cr_obj.risks_and_edge_cases[0].recommendation
                                if cr_obj.risks_and_edge_cases
                                else "Address requested changes before merge."
                            )
                            cr_obj.critical_issues.append(
                                IssueItem(
                                    path="codebase",
                                    line=None,
                                    description=crit_desc,
                                    suggested_fix=crit_fix,
                                )
                            )
                    rendered_body = render_code_review_markdown(cr_obj, enforced_verdict)
                    inline_comments = []
                    if diff_text:
                        cr_issues = list(cr_obj.critical_issues) + list(cr_obj.minor_suggestions)
                        inline_comments, _ = build_github_review_comments(
                            cr_issues,
                            diff_text,
                            existing_comments=existing_comments_data,
                            max_comments=review_budget,
                        )
                    return rendered_body, enforced_verdict, inline_comments
        except Exception as exc:
            logger.debug("Candidate JSON parse attempt skipped: %s", exc)

    # Step 2: Fallback text parsing if no valid JSON object was parsed
    try:
        parsed_dict = parse_text_review_to_dict(body)
        if is_intended_request_changes and not parsed_dict.get("verdict"):
            parsed_dict["verdict"] = "REQUEST_CHANGES"
        normalized_dict = normalize_code_review_dict(parsed_dict)
        cr_obj = CodeReviewResponse.model_validate(normalized_dict)
        enforced_verdict = calculate_strict_verdict(cr_obj)
        if is_intended_request_changes and enforced_verdict == "APPROVE":
            logger.warning(
                "Safety Guardrail: Prevented mechanical upgrade of text REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
            )
            enforced_verdict = "REQUEST_CHANGES"
            if not cr_obj.critical_issues:
                crit_desc = (
                    cr_obj.risks_and_edge_cases[0].risk
                    if cr_obj.risks_and_edge_cases
                    else cr_obj.executive_summary
                )
                crit_fix = (
                    cr_obj.risks_and_edge_cases[0].recommendation
                    if cr_obj.risks_and_edge_cases
                    else "Address requested changes before merge."
                )
                cr_obj.critical_issues.append(
                    IssueItem(
                        path="codebase",
                        line=None,
                        description=crit_desc,
                        suggested_fix=crit_fix,
                    )
                )
        rendered_body = render_code_review_markdown(cr_obj, enforced_verdict)
        inline_comments = []
        if diff_text:
            cr_issues = list(cr_obj.critical_issues) + list(cr_obj.minor_suggestions)
            inline_comments, _ = build_github_review_comments(
                cr_issues,
                diff_text,
                existing_comments=existing_comments_data,
                max_comments=review_budget,
            )
        return rendered_body, enforced_verdict, inline_comments
    except Exception as parse_err:
        logger.warning("Could not parse text review to CodeReviewResponse: %s", parse_err)

    fallback_event = req_event if req_event else "COMMENT"
    if is_intended_request_changes:
        fallback_event = "REQUEST_CHANGES"
    return body, fallback_event, []


def review(
    ctx: Context,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
) -> str:
    """Submit a formal review on a pull request.

    The review findings MUST be supplied as a structured JSON string matching the
    CodeReviewResponse schema (for initial PR reviews) or SyncReviewResponse schema
    (for PR synchronization updates). The final GitHub Markdown review is deterministically
    rendered from the validated schema.

    Args:
        pr_number: Pull request number.
        body: Review payload (strictly a JSON string conforming to CodeReviewResponse or SyncReviewResponse).
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

        # Safety Check: Closed / Merged PR Protection
        raw_state = getattr(pr, "state", None)
        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        is_merged = getattr(pr, "merged", None) is True
        if pr_state == "closed" or is_merged:
            logger.info("PR #%d is closed or merged; skipping review submission", pr_number)
            try:
                from .cancellation import AbortAgentExecution, pr_closed_registry
            except ImportError:
                from webhook_agent.cancellation import AbortAgentExecution, pr_closed_registry

            pr_closed_registry.mark_closed(repo_name, pr_number)
            raise AbortAgentExecution(
                f"PR {repo_name}#{pr_number} is closed or merged. Skipping review submission."
            )

        body, event, inline_comments = _enforce_verdict(body, event, pr)

        try:
            if inline_comments:
                rv = pr.create_review(body=body, event=event, comments=inline_comments)  # type: ignore[arg-type]
            else:
                rv = pr.create_review(body=body, event=event)
        except Exception as review_err:
            if inline_comments:
                logger.warning(
                    "pr.create_review with %d inline comments failed (%s); falling back to body-only review",
                    len(inline_comments),
                    review_err,
                )
                rv = pr.create_review(body=body, event=event)
            else:
                raise

        # Supersede / dismiss prior bot reviews ONLY after new review is created
        existing_reviews = pr.get_reviews()
        for prev_rv in existing_reviews:
            prev_login = (getattr(getattr(prev_rv, "user", None), "login", "") or "").lower()
            if (
                prev_login
                and (
                    prev_login in (BOT_LOGIN.lower(), "hannibal-hub-agents")
                    or prev_login.startswith("hannibal-hub-agents")
                    or prev_login.endswith("[bot]")
                )
                and prev_rv.state in ("CHANGES_REQUESTED", "APPROVED")
                and getattr(prev_rv, "id", None) != getattr(rv, "id", None)
            ):
                try:
                    prev_rv.dismiss("Superseded by fresh code review on latest commit.")
                    logger.info(
                        "Dismissed prior bot review %s on PR #%d",
                        prev_rv.id,
                        pr_number,
                    )
                except Exception as dismiss_err:
                    logger.warning(
                        "Could not dismiss prior bot review %s: %s",
                        prev_rv.id,
                        dismiss_err,
                    )
        _COMMENT_RATE_LIMITER.record(target_key)
        session_state = getattr(ctx, "state", None)
        if isinstance(session_state, dict):
            session_state["review_submitted_in_this_turn"] = True
        detail = getattr(rv, "html_url", str(rv))
        return f"Submitted review ({event}): {detail}"
    except Exception as e:
        if type(e).__name__ == "AbortAgentExecution" or "AbortAgentExecution" in str(type(e)):
            raise
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


# ---------------------------------------------------------------------------
# System instruction for the agent
# ---------------------------------------------------------------------------

# Load templates once at module load time
_PR_TEMPLATE = _load_pr_template()
_CODE_REVIEW_TEMPLATE = _load_code_review_template()
_SYNC_REVIEW_TEMPLATE = _load_sync_review_template()

SYSTEM_INSTRUCTION = """You are a Senior Autonomous Engineer and Code Auditor for the Hannibal Hub ecosystem.

Your core mission is to protect repository hygiene, audit code changes with clinical precision, and generate pristine, actionable technical feedback. Zero sycophancy or generic cheerleading is permitted.

### Reasoning & Grounding Principles

1. **Understand Context**: Analyze user requests, pull request diffs, and codebase structure.
2. **Grounding Pre-Check**: Before claiming that code, teardown blocks, or unit tests are missing in a PR review:
   - You MUST call `read_file()` to inspect target files first.
   - Never suggest creating unit tests or adding cleanup logic without verifying existing tests in `tests/` or teardown blocks in target modules.
3. **Exact Tool Names**: Call tools using their exact function names (e.g., `get_issue`, `read_file`, `add_comment`, `review`) without any `github:` prefix.
4. **Format Results**: Structure reviews, PR descriptions, and responses in Markdown tables, code blocks, and clear sections using the required template.

---

## Code Review Protocol (MANDATORY)

You are a SENIOR ENGINEER performing code reviews, not a cheerleader. Your job is to catch problems, protect code quality, and provide honest, actionable feedback. Agreeing with everything is a failure mode.

### Review Procedure

When reviewing a PR, you MUST:
1. **For Initial PR Creation (`pull_request.opened` or `/review`)**:
   - Evaluate every changed file systematically across **4 Mandatory Audit Dimensions**:
     1) **Logic & Boundaries**: Off-by-one errors, null/None dereferences, unhandled exceptions, resource leaks.
     2) **Concurrency & Memory**: Async race conditions, shared state mutation without locks, memory growth.
     3) **Security & Secrets**: Hardcoded secrets, input sanitization, authentication/authorization boundaries.
     4) **Contract Integrity**: Breaking signature changes, missing invocation site updates across the codebase.
   - Output your review response as a VALID JSON object matching the `CodeReviewResponse` schema with fields: `executive_summary`, `critical_issues`, `minor_suggestions`, `risks_and_edge_cases`, `context_gaps`. When calling `review()`, pass this JSON string as the `body` parameter. Do NOT pass raw Markdown into `review()`; the system deterministically renders clean GitHub Markdown from your validated JSON.
   - For each actionable bug or improvement in `critical_issues` or `minor_suggestions`, specify the exact `path`, `line`, and clinical replacement code in `suggested_fix`. This enables native GitHub Suggested Change inline review comments (` ```suggestion `).

2. **For PR Updates & Re-reviews (`pull_request.synchronize`)**:
   - Review the pre-fetched incremental commit diff (`commit_diff`) and compare it against `previous_bot_reviews`.
   - Output your review response as a VALID JSON object matching the `SyncReviewResponse` schema with fields: `summary`, `resolutions`, `critical_issues`, `minor_suggestions`. When calling `review()`, pass this JSON string as the `body` parameter.
   - For new findings in `critical_issues` or `minor_suggestions`, provide `path`, `line`, and `suggested_fix`.
   - Only track items in `resolutions` that were actually raised as requested changes/findings in `previous_bot_reviews`. If there were no prior review action items or the previous review was APPROVED, leave `resolutions` as an empty list `[]`. Never invent or backfill resolved items from the new commit's changes.
   - For items that were in `previous_bot_reviews`, mark every previously requested issue as `RESOLVED` or `UNRESOLVED` with line citations and evidence.
   - Distinguish PR-authored commits from base branch merges (`Merge branch 'main' ...`). Commits originating from merging or updating from the base branch are part of the target branch and must NOT be attributed to the PR author or flagged as scope creep.

### Verdict Rules (Non-Negotiable)

These rules override your judgment. Apply them mechanically based on your findings:
- ANY critical issue -> event MUST be REQUEST_CHANGES
- 0 critical issues -> event MAY be APPROVE

### Critical Thinking & Anti-Sycophancy Requirements

- **NO SYCOPHANCY / NO CHEERLEADING**: Do NOT use performative praise or generic cheerleading like "Splendid refactoring!", "Exemplary implementation!", or "Rock-solid PR!". State objective technical facts only.
- **HIGH-SIGNAL RISK & EDGE-CASE ANALYSIS**: Highlight genuine potential failure modes, unhandled edge cases, rate limits, timeout risks, or concurrency boundaries when present.
- Every review should aim to include actionable, specific suggestions with file:line citations when improvements are possible.
- Never say code is "verified" without citing specific evidence from the diff for each claim.
- Do not summarize what the code does back to the author — focus on what could go WRONG.
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

### Review Voice & Comment Style (Adapted from adk-samples)

Two registers only for review comments and descriptions:
- **Register A (60%)**: A polite full sentence ending in `?`. (e.g., "Can we use `subprocess.run()` with a list here instead?", "It seems the lock is released before the write finishes. Is that intentional?", "Is there a reason we're not reusing `get_client()` here?")
- **Register B (40%)**: A lowercase fragment, 2-6 words. (e.g., "missing await here", "this'll break if list is empty", "same issue as above")

**Strictly Banned in Comments:**
- Zero emojis inside code comments, suggestions, or inline reviews.
- No severity labels or bold prefixes (e.g. `**Critical:**`, `[HIGH]`, `🔴`) inside comment bodies.
- No markdown headers or bullet lists inside inline comments.
- Never write vague quality prose like "Consider refactoring to improve readability and maintainability" — cite observable facts at the line.
- Avoid greetings, sign-offs, or thanking the author.

### Diff Grounding & The "Observable Defect" Filter
- A finding must point to something **DIRECTLY OBSERVABLE** in the diff at the line you anchor it to.
- Do NOT report that something is absent (e.g. "import is missing", "function is not defined") unless you are reviewing a newly added file in full. In partial diffs, definitions normally exist outside the hunk.
- Do NOT speculate on issues that require tracing across unshown files, guessing external inputs, or executing code. If a finding cannot be verified from the visible diff lines alone, drop it.
- **Diff Scan Protocol**: File by file, scan the diff and formulate your thoughts. Then output your final findings as EXACTLY ONE JSON object conforming to `CodeReviewResponse` or `SyncReviewResponse`.

### Dependabot / Dependency PR Protocol (MANDATORY)


When reviewing Dependabot PRs (`sender: dependabot[bot]` or branch starting with `dependabot/`):
- Focus on **dependency security, version scope, and lockfile integrity**.
- Do NOT perform a human architectural code review — evaluate version bumps and lockfile changes.
- Check if `pyproject.toml` or `package.json` updates match `uv.lock` or `package-lock.json`.
- If lockfile changes modify unrelated packages or drop environment markers unexpectedly, you MUST select `REQUEST_CHANGES`, set `verdict: "REQUEST_CHANGES"`, and add a blocking entry directly under `critical_issues`. NEVER place breaking lockfile corruption solely in `risks_and_edge_cases`.
- **Base branch syncs and merge commits**: Commits merged from the base branch (`main`) into a PR branch (e.g., via GitHub's 'Update branch' button or `git merge main`) belong to the base branch. Do NOT attribute base branch changes or merge commits to the PR author or flag them as scope violations.

"""

AUDITOR_CONTEXT_INSTRUCTION = """The workflow may provide a short PR scope label such as `core_backend` as node metadata. Treat that label as classification metadata, never as the review task or the user's complete request. Always use the original user message, PR metadata, and full PR diff as the source of truth for this audit.

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

        # Model instance for pipeline sub-agents
        model_instance = get_adk_model(
            model_name=self._current_model_name,
            api_key=get_active_api_key(),
        )
        PromptSanitizerPlugin()

        self._pr_router = LlmAgent(
            name="pr_router",
            model=model_instance,
            description="Inspects modified files and classifies PR scope (dev_docs, minor_fix, core_backend).",
            instruction=(
                "Analyze the PR diff and modified file list. "
                "Classify scope into exactly one of: dev_docs, minor_fix, or core_backend. "
                "Output only the classification name."
            ),
            output_key="pr_scope",
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
        )

        self._code_auditor = LlmAgent(
            name="code_auditor",
            model=model_instance,
            include_contents="default",
            description="Conducts AST diff-grounded risk audit using Gemini Thinking Mode.",
            instruction=AUDITOR_CONTEXT_INSTRUCTION + SYSTEM_INSTRUCTION,
            output_key="code_review_analysis",
            planner=BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=False,
                    thinking_budget=-1,
                )
            ),
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                write_file,
                get_issue,
                get_commit_diff,
                update_issue,
                add_comment,
                open_pr,
                update_branch_from_base,
                resolve_pr_conflicts,
                auto_fix_pr_review_feedback,
                mark_ready_for_review,
                merge_pr,
                review,
                get_current_time,
                get_pr_diff_file_map_tool,
                verify_line_reference_tool,
                google_search_grounding_tool,
            ],
        )

        self._verdict_agent = LlmAgent(
            name="verdict_agent",
            model=model_instance,
            include_contents="none",
            description="Produces structured AuditVerdict JSON output.",
            instruction="""You are the Chief Auditor synthesizing final verdicts for Pull Requests.
Evaluate the classified PR scope and the code auditor's technical findings:

### PR Scope
{pr_scope}

### Audit Analysis & Findings
{code_review_analysis}

Synthesize these findings into an AuditVerdict structured JSON payload matching the schema.
Clean dev/docs PRs return risks: [].
""",
            output_schema=AuditVerdict,
            output_key="audit_verdict",
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
        )

        self._agent = Workflow(
            name="webhook_agent",
            edges=[
                (START, self._pr_router),
                (self._pr_router, self._code_auditor),
                (self._code_auditor, self._verdict_agent),
            ],
        )

        self._history_pruning_plugin = WebhookHistoryPruningPlugin(max_events=12)
        self._tool_pruning_plugin = ToolOutputPruningPlugin()

        self._app = App(
            name=self._app_name,
            root_agent=self._agent,
            plugins=[
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
            ],
        )

        # Create the runner
        self._runner = Runner(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _advance_model_chain(self, error: Exception | None = None) -> str | None:
        """Cascade to next model in TPM descending chain on rate limit or server error."""
        # Mark current model depleted with smart metric-aware cooldown parsing
        _DEPLETED_MODEL_REGISTRY.mark_depleted(self._current_model_name, error=error)

        # Refresh model chain to get available non-depleted models
        full_chain = get_model_chain()
        curr_norm = self._current_model_name.replace("models/", "").strip().lower()
        available = [m for m in full_chain if m.replace("models/", "").strip().lower() != curr_norm]
        self._model_chain = available if available else full_chain
        self._chain_index = 0

        if self._model_chain:
            next_model = self._model_chain[0]
            logger.warning(
                "⚠️ Cascading model chain from %s -> %s",
                self._current_model_name,
                next_model,
            )
            self._current_model_name = next_model
            new_model_instance = get_adk_model(
                model_name=next_model,
                api_key=get_active_api_key(),
            )
            self._pr_router.model = new_model_instance
            self._code_auditor.model = new_model_instance
            self._verdict_agent.model = new_model_instance
            self._runner = Runner(
                app=self._app,
                session_service=self._session_service,
                memory_service=self._memory_service,
            )
            return next_model
        return None

    def _create_fallback_agent(self, error: Exception | None = None) -> None:
        """Switch to fallback model when primary model is unavailable."""
        self._advance_model_chain(error=error)

        # Recreate runner with new agent
        self._runner = Runner(
            app=self._app,
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
            pr_num = pr.get("number", "unknown")
            parts.append(f"PR Number: {pr_num}")
            parts.append(f"PR Title: {pr.get('title', 'N/A')}")
            parts.append(f"PR Body: {(pr.get('body') or '')[:500]}")
            parts.append(f"PR Head Branch: {(pr.get('head') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Base Branch: {(pr.get('base') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Additions: {pr.get('additions', 'N/A')}")
            parts.append(f"PR Deletions: {pr.get('deletions', 'N/A')}")
            parts.append(f"PR Changed Files: {pr.get('changed_files', 'N/A')}")

            if canonical == "pull_request.synchronize" or raw.get("action") == "synchronize":
                before_sha = raw.get("before", "")
                head_sha = (pr.get("head") or {}).get("sha", "")
                parts.append(
                    f"\n[NEW COMMIT PUSHED - Event: pull_request.synchronize]\n"
                    f"Before SHA: {before_sha}\n"
                    f"Head SHA: {head_sha}\n"
                    f"MANDATORY INSTRUCTION: A new commit was pushed to PR #{pr_num}. "
                    f"Review the pre-fetched incremental commit diff and full PR diff below to evaluate "
                    f"the changes in turn 1 and submit an updated formal review (APPROVE or REQUEST_CHANGES)."
                )
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

        # Include pre-fetched commit diff (incremental changes) if available
        if "commit_diff" in raw:
            parts.append(f"\nNew Commit Diff (Incremental Changes):\n{raw['commit_diff']}")

        # Include PR diff (full accumulated state) if available
        if "pr_diff" in raw:
            parts.append(f"\nFull PR Diff (Accumulated State):\n{raw['pr_diff']}")

        # Include pre-fetched inline comment code context if available
        if "inline_code_context" in raw:
            parts.append(f"\nPre-Fetched Inline Code Context:\n{raw['inline_code_context']}")

        # Include pre-executed conflict resolution result if available
        if "conflict_resolution_result" in raw:
            res = raw["conflict_resolution_result"]
            parts.append(
                f"\nPre-Executed Conflict Resolution Result:\n"
                f"Status: {'Success' if res.get('success') else 'Failed'}\n"
                f"Detail: {res.get('detail') or res.get('error') or 'N/A'}"
            )

        # Include pre-fetched commit history summary if available
        if "commit_history_summary" in raw:
            parts.append(f"\nPre-Fetched Commit History Summary:\n{raw['commit_history_summary']}")

        # Include pre-fetched previous bot reviews if available
        if "previous_bot_reviews" in raw:
            parts.append(f"\nPre-Fetched Previous Bot Reviews:\n{raw['previous_bot_reviews']}")
            if not raw.get("prior_reviews_had_request_changes", False):
                parts.append(
                    "\nNOTE ON RESOLUTION TRACKER: No prior review requested changes on this PR. "
                    "You MUST leave 'resolutions' as an empty list ([]) in SyncReviewResponse. "
                    "Do NOT invent or backfill resolved items."
                )

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

        # Short-circuit execution if PR is closed or merged
        raw = event_data.get("raw_payload") or {}
        pr_data = raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request") or {}
        if isinstance(raw.get("issue"), dict) and not pr_data:
            pr_data = raw.get("issue") or {}

        repo_name = (
            event_data.get("repo_name") or (raw.get("repository") or {}).get("full_name") or ""
        )
        pr_number = pr_data.get("number") if isinstance(pr_data, dict) else None

        try:
            from .cancellation import pr_closed_registry
        except ImportError:
            from webhook_agent.cancellation import pr_closed_registry

        is_registry_closed = bool(
            repo_name and pr_number and pr_closed_registry.is_closed(repo_name, int(pr_number))
        )

        raw_state = pr_data.get("state") if isinstance(pr_data, dict) else ""
        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        merged_at_val = pr_data.get("merged_at") if isinstance(pr_data, dict) else None
        is_merged = (
            pr_data.get("merged") is True
            or (isinstance(merged_at_val, str) and bool(merged_at_val.strip()))
            if isinstance(pr_data, dict)
            else False
        )
        if pr_state == "closed" or is_merged or is_registry_closed:
            logger.info(
                "🔒 PR is closed or merged (state=%s, merged=%s, registry=%s); short-circuiting execution",
                pr_state,
                is_merged,
                is_registry_closed,
            )
            return [
                ActionResult(
                    tool="skip_closed_pr",
                    success=True,
                    detail=f"PR is closed/merged (state={pr_state}, merged={is_merged}, registry={is_registry_closed}); agent execution skipped.",
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

        # Programmatic Command Router: Intercept /resolve slash command for instant Git Worktree conflict resolution
        raw = event_data.get("raw_payload", {})
        comment_body = ""
        if isinstance(raw, dict) and isinstance(raw.get("comment"), dict):
            comment_body = (raw["comment"].get("body") or "").strip()

        if "/resolve" in comment_body.lower():
            if isinstance(raw, dict) and "conflict_resolution_result" in raw:
                res = raw["conflict_resolution_result"]
                return [
                    ActionResult(
                        tool="resolve_merge_conflicts",
                        success=res.get("success", False),
                        detail=res.get("detail", ""),
                    )
                ]

            pr_number = None
            if isinstance(raw, dict):
                pr_number = (raw.get("pull_request") or {}).get("number") or (
                    raw.get("issue") or {}
                ).get("number")

            if pr_number:
                selected_model = _select_model_for_event(event_data)
                logger.info(
                    "⚡ Programmatic Command Router: Intercepted /resolve for PR #%d (trace: %s, model: %s)",
                    pr_number,
                    trace_id[-4:],
                    selected_model,
                )
                try:
                    repo = gh_client.get_repo(repo_full_name)
                    pr = repo.get_pull(pr_number)
                    genai_client = get_shared_genai_client()
                    token = getattr(getattr(gh_client, "_auth", None), "token", None)
                    res = resolve_merge_conflicts(
                        pr_number=pr_number,
                        head_branch=pr.head.ref,
                        base_branch=pr.base.ref,
                        genai_client=genai_client,
                        model_name=selected_model,
                        token=token,
                    )
                    status_detail = res.get("detail", "")
                    if res.get("success"):
                        comment_text = (
                            f"I have surgically resolved the merge conflicts for PR #{pr_number} "
                            f"against `{pr.base.ref}` using an isolated Git Worktree and pushed the updated branch.\n\n"
                            f"**Detail:** {status_detail}"
                        )
                    else:
                        comment_text = (
                            f"Unable to automatically resolve merge conflicts for PR #{pr_number}.\n\n"
                            f"**Detail:** {status_detail}"
                        )
                    pr.create_issue_comment(comment_text)
                    return [
                        ActionResult(
                            tool="resolve_merge_conflicts",
                            success=res.get("success", False),
                            detail=status_detail,
                        )
                    ]
                except Exception as exc:
                    logger.exception(
                        "Programmatic /resolve execution failed for PR #%d: %s",
                        pr_number,
                        exc,
                    )
                    return [
                        ActionResult(
                            tool="resolve_merge_conflicts",
                            success=False,
                            detail=f"Programmatic /resolve failed: {exc}",
                        )
                    ]

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
            len(getattr(user_message.parts[0], "text", "") or "") if user_message.parts else 0,
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
            new_model_instance = get_adk_model(
                model_name=selected_model,
                api_key=get_active_api_key(),
            )
            self._pr_router.model = new_model_instance
            self._code_auditor.model = new_model_instance
            self._verdict_agent.model = new_model_instance

        # Run the agent asynchronously with retry and fallback support
        results: list[ActionResult] = []
        emitted_texts: list[str] = []
        final_session = None

        async def _execute_agent() -> None:
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                # Handle token recording if usage metadata is available
                if hasattr(event, "usage_metadata") and event.usage_metadata:
                    total_tok = getattr(event.usage_metadata, "total_token_count", 0) or getattr(
                        event.usage_metadata, "total_tokens", 0
                    )
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
                                    tool=str(response.name or ""),
                                    success=True,
                                    detail=f"tool executed: {response.response}",
                                )
                            )

                # Handle text responses — log the agent's reasoning (filtering out thought tokens)
                if (
                    event.content
                    and event.content.parts
                    and any(
                        hasattr(p, "text") and p.text and not getattr(p, "thought", False)
                        for p in event.content.parts
                    )
                ):
                    for part in event.content.parts:
                        if getattr(part, "thought", False):
                            continue
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

        async def _run() -> None:
            nonlocal results, final_session
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
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + notice

                        previous_critique = session.state.get("last_review_critique", "")
                        if previous_critique:
                            critique_notice = (
                                f"\n\n[YOUR PREVIOUS REVIEW CRITIQUE]:\n{previous_critique}\n"
                                "Verify line-by-line which specific items were resolved by the new commit."
                            )
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + critique_notice

                    # Set user_state values - they get merged into session.state by InMemorySessionService
                    # This is needed because session copies are returned and our direct mutations wouldn't persist
                    self._session_service.user_state.setdefault(self._app_name, {}).setdefault(
                        user_id, {}
                    )["gh_client"] = gh_client
                    self._session_service.user_state.setdefault(self._app_name, {}).setdefault(
                        user_id, {}
                    )["repo_full_name"] = repo_full_name
                    self._session_service.user_state.setdefault(self._app_name, {}).setdefault(
                        user_id, {}
                    )["sender"] = user_id

                    # Execute the ADK runner with current model
                    await _execute_agent()

                    # Fetch the final session state with all sub-agent output_key updates
                    final_session = await self._session_service.get_session(
                        app_name=self._app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    return  # Success - exit the retry loop

                except Exception as e:
                    if type(e).__name__ == "AbortAgentExecution" or "AbortAgentExecution" in str(
                        type(e)
                    ):
                        logger.info(
                            "🔒 Agent execution short-circuited (trace: %s): %s",
                            trace_id[-4:],
                            e,
                        )
                        results.append(
                            ActionResult(
                                tool="skip_closed_pr",
                                success=True,
                                detail=f"Execution short-circuited: {e}",
                            )
                        )
                        return

                    last_error = e
                    if _is_transient_error(e) and attempt < _MAX_RETRIES - 1:
                        rate_details = extract_rate_limit_details(e)
                        self._advance_model_chain(error=e)
                        err_s = str(e).lower()
                        is_503_high_demand = (
                            "503" in err_s or "unavailable" in err_s or "high demand" in err_s
                        )
                        retry_delay = (
                            0.5
                            if is_503_high_demand
                            else min(rate_details.get("retry_after_seconds") or 2.0, 10.0)
                        )
                        logger.warning(
                            "Transient error on attempt %d/%d (trace: %s): %s. Active model failover -> %s (delay: %.1fs)",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            e,
                            self._current_model_name,
                            retry_delay,
                        )
                        if retry_delay > 0:
                            await asyncio.sleep(retry_delay)
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

        # Deterministic review submission: if no review tool was called during a PR review event,
        # extract structured audit state from ADK session or emitted texts, and enforce verdict.
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})
        comment_body = (
            (raw.get("comment", {}) or {}).get("body", "") if isinstance(raw, dict) else ""
        )
        is_pr_review_event = (
            canonical.startswith(("pull_request.", "pull_request_review"))
            or "/review" in comment_body
        )
        has_review_action = any(r.tool == "review" for r in results)

        if is_pr_review_event and not has_review_action:
            # 1. Safely extract review payload from session state or emitted text
            review_payload = ""
            if final_session and final_session.state:
                raw_verdict = final_session.state.get("audit_verdict")
                raw_analysis = final_session.state.get("code_review_analysis")

                if raw_verdict:
                    if hasattr(raw_verdict, "model_dump_json"):
                        review_payload = raw_verdict.model_dump_json()
                    elif isinstance(raw_verdict, dict):
                        import json

                        review_payload = json.dumps(raw_verdict)
                    else:
                        review_payload = str(raw_verdict)
                elif raw_analysis:
                    review_payload = str(raw_analysis)

            if not review_payload and emitted_texts:
                full_text = "\n\n".join(emitted_texts)
                is_review_content = (
                    "Scorecard" in full_text
                    or "| Category |" in full_text
                    or "Verdict:" in full_text
                    or '"verdict"' in full_text
                    or '"executive_summary"' in full_text
                    or '"critical_issues"' in full_text
                    or '"resolutions"' in full_text
                    or "Code Review" in full_text
                )
                if is_review_content:
                    review_payload = full_text

            if review_payload:
                pr_number = None
                if isinstance(raw, dict):
                    pr_number = (raw.get("pull_request") or {}).get("number") or (
                        raw.get("issue") or {}
                    ).get("number")

                if pr_number:
                    try:
                        repo = gh_client.get_repo(repo_full_name)
                        pr = repo.get_pull(pr_number)
                        body, enforced_event, inline_comments = _enforce_verdict(
                            review_payload, "COMMENT", pr
                        )
                        try:
                            if inline_comments:
                                rv = pr.create_review(
                                    body=body,
                                    event=enforced_event,
                                    comments=inline_comments,  # type: ignore[arg-type]
                                )
                            else:
                                rv = pr.create_review(body=body, event=enforced_event)
                        except Exception as fb_err:
                            if inline_comments:
                                logger.warning(
                                    "Review with inline comments failed (%s); falling back to body-only review",
                                    fb_err,
                                )
                                rv = pr.create_review(body=body, event=enforced_event)
                            else:
                                raise

                        detail = getattr(rv, "html_url", str(rv))
                        results.append(
                            ActionResult(
                                tool="review",
                                success=True,
                                detail=f"Deterministic review submitted ({enforced_event}): {detail}",
                            )
                        )
                        logger.info(
                            "Deterministic review submitted for PR #%d (%s)",
                            pr_number,
                            enforced_event,
                        )
                    except Exception as fallback_err:
                        logger.warning(
                            "Deterministic review submission failed: %s",
                            fallback_err,
                        )

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id[-4:],
            )

        return results
