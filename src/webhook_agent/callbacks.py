"""ADK Callbacks Suite for Webhook Agent.

Provides native ADK lifecycle callbacks for:
- Pre-flight free count_tokens API metering, Free Tier TPM chunking (<15k), and rate limit waiting (before_model_callback).
- Post-call token usage auditing (after_model_callback).
- State pre-population with PR metadata and active tier (before_agent_callback).
- Tool parameter validation & sanitization (before_tool_callback).
- Self-healing error recovery (on_tool_error_callback).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import BaseTool, ToolContext

from webhook_agent.logic.rate_limiter import _resolve_tier, get_active_api_key, rpm_waiter

logger = logging.getLogger("webhook_agent.callbacks")


MUTATING_TOOLS: set[str] = {
    "review",
    "add_comment",
    "merge_pr",
    "open_pr",
    "update_issue",
    "update_branch_from_base",
    "resolve_pr_conflicts",
    "auto_fix_pr_review_feedback",
    "mark_ready_for_review",
}


def _check_pr_closed_short_circuit(state: Any) -> None:
    """Check if target PR is registered as closed/merged and abort agent turn immediately."""
    repo_full_name = state.get("repo_full_name") or ""
    pr_number = state.get("pr_number") or state.get("issue_number")
    if repo_full_name and pr_number:
        try:
            from .cancellation import AbortAgentExecution, pr_closed_registry

            if pr_closed_registry.is_closed(str(repo_full_name), int(pr_number)):
                logger.info(
                    "🔒 Short-circuiting agent turn: PR %s#%s is marked CLOSED",
                    repo_full_name,
                    pr_number,
                )
                raise AbortAgentExecution(
                    f"PR {repo_full_name}#{pr_number} is closed or merged. Short-circuiting execution."
                )
        except ImportError:
            pass


async def before_agent_callback(callback_context: CallbackContext) -> None:
    """Pre-populate session state with active tier and runtime context before agent execution."""
    _check_pr_closed_short_circuit(callback_context.state)
    active_tier = _resolve_tier()
    callback_context.state["active_tier"] = active_tier
    callback_context.state["review_submitted_in_this_turn"] = False
    callback_context.state["mutating_tool_executed_in_this_turn"] = False
    callback_context.state.setdefault("tools_executed", [])
    agent_name = getattr(callback_context, "agent_name", None) or getattr(
        getattr(callback_context, "agent", None), "name", "unknown_agent"
    )
    logger.info("🤖 [SubAgent: %s] Starting sub-agent execution...", agent_name)
    logger.debug(
        "before_agent_callback: initialized active_tier=%s in state for sub-agent '%s'",
        active_tier,
        agent_name,
    )


async def before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Execute pre-flight token metering, dynamic model TPM chunking, and rate limit waiting."""
    _check_pr_closed_short_circuit(callback_context.state)
    active_tier = callback_context.state.get("active_tier") or _resolve_tier()
    api_key = get_active_api_key()
    try:
        from webhook_agent.webhook_agent import get_active_model

        default_model = get_active_model()
    except ImportError:
        default_model = "gemini-3.8-flash"
    target_model = getattr(llm_request, "model", None) or default_model

    input_text = ""
    if hasattr(llm_request, "contents") and llm_request.contents:
        input_text = str(llm_request.contents)

    exact_tokens = len(input_text) // 4 + 500
    used_heuristic = True
    if exact_tokens > 0 and api_key:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            resp = client.models.count_tokens(model=target_model, contents=input_text)
            if resp and resp.total_tokens:
                exact_tokens = int(resp.total_tokens)
                used_heuristic = False
        except Exception:
            pass

    # Apply safety margin when using heuristic (char-to-token ratio is unreliable)
    if used_heuristic:
        exact_tokens = int(exact_tokens * 1.5)

    await rpm_waiter.check_and_wait(
        model=target_model,
        estimated_tokens=exact_tokens,
        tier=active_tier,
    )
    with contextlib.suppress(Exception):
        setattr(llm_request, "_rate_limit_checked", True)  # noqa: B010

    # Context Caching Model Guard: Strictly only Gemini 3+ models support context caching.
    # Gemma models (e.g. gemma-4-31b-it) and pre-Gemini-3 models do NOT support caching.
    from webhook_agent.logic.model_chain import is_gemini_3_plus

    if not is_gemini_3_plus(target_model):
        if hasattr(llm_request, "cache_config"):
            llm_request.cache_config = None
        if hasattr(llm_request, "cache_metadata"):
            llm_request.cache_metadata = None
        if hasattr(llm_request, "cacheable_contents_token_count"):
            llm_request.cacheable_contents_token_count = None
    else:
        # For Gemini 3+, ensure context cache config is active with 4096-token floor
        if getattr(llm_request, "cache_config", None) is None:
            with contextlib.suppress(Exception):
                from google.adk.agents.context_cache_config import ContextCacheConfig

                llm_request.cache_config = ContextCacheConfig(
                    min_tokens=4096,
                    ttl_seconds=1800,
                    cache_intervals=10,
                )

    callback_context.state["prompt_tokens"] = exact_tokens
    callback_context.state["active_model"] = target_model
    return None


def _extract_total_tokens(llm_response: LlmResponse) -> int:
    """Extract total token count resiliently across SDK object and dictionary schemas."""
    usage = getattr(llm_response, "usage_metadata", None)
    if not usage:
        return 0

    # 1. Attribute access (Pydantic / SDK dataclass)
    for attr in ("total_token_count", "total_tokens"):
        val = getattr(usage, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)

    # 2. Dictionary access (serialized JSON / dict payload)
    if isinstance(usage, dict):
        for key in ("total_token_count", "total_tokens"):
            val = usage.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

    return 0


def _extract_cached_tokens(llm_response: LlmResponse) -> int:
    """Extract cached token count resiliently across all Google GenAI / Vertex AI SDK variants."""
    usage = getattr(llm_response, "usage_metadata", None)
    if usage:
        # 1. Direct attribute access
        for attr in ("cached_content_token_count", "cached_tokens", "cache_token_count"):
            val = getattr(usage, attr, None)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

        # 2. Dictionary-like access
        if isinstance(usage, dict):
            for key in ("cached_content_token_count", "cached_tokens", "cache_token_count"):
                val = usage.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    return int(val)

        # 3. Check nested cache_tokens_details (found in google-genai / Vertex AI schemas)
        details = getattr(usage, "cache_tokens_details", None)
        if isinstance(usage, dict) and not details:
            details = usage.get("cache_tokens_details")
        if details:
            for attr in ("cached_tokens", "cached_content_token_count", "token_count"):
                val = (
                    getattr(details, attr, None)
                    if not isinstance(details, dict)
                    else details.get(attr)
                )
                if isinstance(val, (int, float)) and val > 0:
                    return int(val)

    return 0


async def after_model_callback(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Record token usage metadata and sanitize hallucinated tool prefixes after Gemini responds."""
    total_tokens = _extract_total_tokens(llm_response)
    cached_tokens = _extract_cached_tokens(llm_response)

    # Track cache utilization from either token metrics or active CacheMetadata
    cache_meta = getattr(llm_response, "cache_metadata", None)
    active_cache_name = getattr(cache_meta, "cache_name", None) if cache_meta else None

    if total_tokens > 0:
        callback_context.state["total_tokens"] = total_tokens
    if cached_tokens > 0:
        callback_context.state["cached_content_tokens"] = cached_tokens
        logger.info(
            "💎 [Gemini Context Cache HIT] %d cached tokens reused for model %s",
            cached_tokens,
            callback_context.state.get("active_model", "gemini-3"),
        )
    elif active_cache_name:
        callback_context.state["active_cache_name"] = active_cache_name
        logger.info(
            "💎 [Gemini Context Cache Reused] Active cache %s attached for model %s",
            active_cache_name,
            callback_context.state.get("active_model", "gemini-3"),
        )

    logger.debug(
        "after_model_callback: recorded total_tokens=%d cached_tokens=%d cache_name=%s",
        total_tokens,
        cached_tokens,
        active_cache_name,
    )

    if total_tokens > 0:
        target_model = callback_context.state.get("active_model")
        if not target_model:
            try:
                from webhook_agent.webhook_agent import get_active_model

                target_model = get_active_model()
            except ImportError:
                target_model = "gemini-3.8-flash"
        await rpm_waiter.record_actual_tokens(
            model=target_model,
            actual_tokens=int(total_tokens),
        )

    # Sanitize hallucinated 'github:' tool prefixes from LLM response before ADK tool lookup
    if hasattr(llm_response, "content") and llm_response.content:
        parts = getattr(llm_response.content, "parts", None) or []
        for part in parts:
            func_call = getattr(part, "function_call", None)
            if func_call:
                name = getattr(func_call, "name", "") or ""
                if name.startswith("github:"):
                    clean_name = name.removeprefix("github:")
                    logger.info(
                        "Sanitized hallucinated tool prefix: '%s' -> '%s'",
                        name,
                        clean_name,
                    )
                    func_call.name = clean_name
    return None


async def before_tool_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Validate and sanitize tool arguments before execution."""
    _check_pr_closed_short_circuit(tool_context.state)
    if "pr_number" in args and isinstance(args["pr_number"], str):
        with contextlib.suppress(ValueError):
            args["pr_number"] = int(args["pr_number"])

    # Track executed tools in session state for downstream audit gates
    tools_executed = tool_context.state.setdefault("tools_executed", [])
    if isinstance(tools_executed, list):
        if tool.name not in tools_executed:
            tools_executed.append(tool.name)
    elif isinstance(tools_executed, set):
        tools_executed.add(tool.name)

    return None


async def on_tool_error_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception
) -> dict[str, Any] | None:
    """Self-healing error recovery callback."""
    logger.warning("on_tool_error_callback: tool '%s' raised error: %s", tool.name, error)
    if tool.name == "update_branch_from_base":
        pr_number = args.get("pr_number") or args.get("number")
        if pr_number:
            tool_context.state["trigger_worktree_conflict_resolution"] = True
            return {
                "success": False,
                "detail": f"REST API auto-merge failed for PR #{pr_number}. Triggering isolated Git Worktree conflict resolution.",
            }

    err_str = str(error).lower()
    if "429" in err_str or "resource_exhausted" in err_str:
        return {
            "success": False,
            "detail": f"Tool '{tool.name}' experienced a temporary limit or error ({error}). Audit proceeding using remaining available tools.",
        }

    return None


async def after_tool_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> Any:
    """Pass tool response directly without truncation."""
    _check_pr_closed_short_circuit(tool_context.state)
    return tool_response
