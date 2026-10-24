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

from webhook_agent.core.policy import Mode
from webhook_agent.models.rate_limiter import _resolve_tier, rpm_waiter

logger = logging.getLogger("webhook_agent.core.callbacks")


def _check_pr_closed_short_circuit(state: Any) -> None:
    """Check if target PR is registered as closed/merged and abort agent turn immediately."""
    repo_full_name = state.get("repo_full_name") or ""
    pr_number = state.get("pr_number") or state.get("issue_number")
    if repo_full_name and pr_number:
        try:
            from webhook_agent.core.cancellation import (
                AbortAgentExecution,
                pr_closed_registry,
            )

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

    # Prevent cross-delivery session state leaks when session IDs are reused across a PR lifetime
    from webhook_agent.core.guardrails import reset_guardrail_state

    reset_guardrail_state(callback_context.state)

    callback_context.state.setdefault("tools_executed", [])
    agent_name = getattr(callback_context, "agent_name", None) or getattr(
        getattr(callback_context, "agent", None), "name", "unknown_agent"
    )
    logger.debug("🤖 [SubAgent: %s] Starting sub-agent execution...", agent_name)
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
    try:
        from webhook_agent.models.model_chain import get_active_model

        default_model = get_active_model()
    except ImportError:
        default_model = "gemini-3.8-flash"
    target_model = getattr(llm_request, "model", None) or default_model

    input_text = ""
    if hasattr(llm_request, "contents") and llm_request.contents:
        input_text = str(llm_request.contents)

    exact_tokens = len(input_text) // 4 + 500
    used_heuristic = True
    if exact_tokens > 0:
        try:
            from webhook_agent.core.loop_helpers import get_shared_genai_client

            client = get_shared_genai_client()
            if client:
                resp = client.models.count_tokens(model=target_model, contents=input_text)
                if resp and resp.total_tokens:
                    exact_tokens = int(resp.total_tokens)
                    used_heuristic = False
        except Exception as err:
            logger.debug("Exact token counting failed, falling back to heuristic: %s", err)

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
    from webhook_agent.models.model_chain import is_gemini_3_plus

    if not is_gemini_3_plus(target_model):
        if hasattr(llm_request, "cache_config"):
            llm_request.cache_config = None
        if hasattr(llm_request, "cache_metadata"):
            llm_request.cache_metadata = None
        if hasattr(llm_request, "cacheable_contents_token_count"):
            llm_request.cacheable_contents_token_count = None
        clean_target = (target_model or "").replace("models/", "").strip().lower()
        if "gemma" in clean_target and hasattr(llm_request, "config") and llm_request.config:
            if getattr(llm_request.config, "thinking_config", None) is not None:
                llm_request.config.thinking_config = None
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


def _extract_prompt_tokens(llm_response: LlmResponse) -> int:
    """Extract prompt token count resiliently across SDK object and dictionary schemas."""
    usage = getattr(llm_response, "usage_metadata", None)
    if not usage:
        return 0

    # 1. Attribute access (Pydantic / SDK dataclass)
    for attr in ("prompt_token_count", "prompt_tokens", "input_tokens", "input_token_count"):
        val = getattr(usage, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)

    # 2. Dictionary access (serialized JSON / dict payload)
    if isinstance(usage, dict):
        for key in ("prompt_token_count", "prompt_tokens", "input_tokens", "input_token_count"):
            val = usage.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

    return 0


def _extract_candidates_tokens(llm_response: LlmResponse) -> int:
    """Extract output/candidate token count resiliently across SDK object and dictionary schemas."""
    usage = getattr(llm_response, "usage_metadata", None)
    if not usage:
        return 0

    # 1. Attribute access (Pydantic / SDK dataclass)
    for attr in (
        "candidates_token_count",
        "candidates_tokens",
        "output_tokens",
        "output_token_count",
        "completion_tokens",
    ):
        val = getattr(usage, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)

    # 2. Dictionary access (serialized JSON / dict payload)
    if isinstance(usage, dict):
        for key in (
            "candidates_token_count",
            "candidates_tokens",
            "output_tokens",
            "output_token_count",
            "completion_tokens",
        ):
            val = usage.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

    return 0


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
    prompt_tokens = _extract_prompt_tokens(llm_response)
    candidates_tokens = _extract_candidates_tokens(llm_response)
    total_tokens = _extract_total_tokens(llm_response)
    cached_tokens = _extract_cached_tokens(llm_response)

    # Track cache utilization from either token metrics or active CacheMetadata
    cache_meta = getattr(llm_response, "cache_metadata", None)
    active_cache_name = getattr(cache_meta, "cache_name", None) if cache_meta else None

    target_model = callback_context.state.get("active_model", "gemini-3")
    repo = callback_context.state.get("repo_full_name", "")
    item_id = callback_context.state.get("pr_number") or callback_context.state.get(
        "issue_number", ""
    )
    tier = callback_context.state.get("active_tier", "")
    logger.info(
        "📊 [LLM Token Usage] model=%s, total=%d, prompt=%d, candidates=%d, cached=%d",
        target_model,
        total_tokens,
        prompt_tokens,
        candidates_tokens,
        cached_tokens,
        extra={
            "json_fields": {
                "event_type": "llm_token_usage",
                "model": target_model,
                "total_tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "candidates_tokens": candidates_tokens,
                "cached_tokens": cached_tokens,
                "repo": repo,
                "item_id": item_id,
                "tier": tier,
            }
        },
    )

    if total_tokens > 0:
        callback_context.state["total_tokens"] = total_tokens
    if prompt_tokens > 0:
        callback_context.state["prompt_tokens"] = prompt_tokens
    if candidates_tokens > 0:
        callback_context.state["candidates_tokens"] = candidates_tokens
    if cached_tokens > 0:
        callback_context.state["cached_content_tokens"] = cached_tokens
        logger.info(
            "💎 [Gemini Context Cache HIT] %d cached tokens reused for model %s",
            cached_tokens,
            target_model,
            extra={
                "json_fields": {
                    "event_type": "cache_hit",
                    "model": target_model,
                    "cached_tokens": cached_tokens,
                    "repo": repo,
                    "item_id": item_id,
                    "tier": tier,
                }
            },
        )
    elif active_cache_name:
        callback_context.state["active_cache_name"] = active_cache_name
        logger.info(
            "💎 [Gemini Context Cache Reused] Active cache %s attached for model %s",
            active_cache_name,
            target_model,
            extra={
                "json_fields": {
                    "event_type": "cache_reused",
                    "model": target_model,
                    "cache_name": active_cache_name,
                    "repo": repo,
                    "item_id": item_id,
                    "tier": tier,
                }
            },
        )

    logger.debug(
        "after_model_callback: recorded total_tokens=%d prompt_tokens=%d candidates_tokens=%d cached_tokens=%d cache_name=%s",
        total_tokens,
        prompt_tokens,
        candidates_tokens,
        cached_tokens,
        active_cache_name,
    )

    if total_tokens > 0:
        target_model = callback_context.state.get("active_model")
        if not target_model:
            try:
                from webhook_agent.models.model_chain import get_active_model

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
                    logger.debug(
                        "Sanitized hallucinated tool prefix: '%s' -> '%s'",
                        name,
                        clean_name,
                    )
                    func_call.name = clean_name
    return None


_SENSITIVE_ARG_SUBSTRINGS = ("token", "key", "secret", "password", "auth", "credential")


def _format_tool_args_for_logging(args: dict[str, Any], max_val_len: int = 120) -> dict[str, Any]:
    """Format tool argument key-value pairs safely for operational logging."""
    formatted: dict[str, Any] = {}
    for k, v in args.items():
        k_lower = k.lower()
        if any(sens in k_lower for sens in _SENSITIVE_ARG_SUBSTRINGS):
            formatted[k] = "[REDACTED]"
            continue

        v_repr = repr(v)
        if len(v_repr) > max_val_len:
            formatted[k] = (
                v_repr[:max_val_len] + f"... [truncated {len(v_repr) - max_val_len} chars]"
            )
        else:
            formatted[k] = v
    return formatted


async def before_tool_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Validate and sanitize tool arguments before execution."""
    _check_pr_closed_short_circuit(tool_context.state)
    if "pr_number" in args and isinstance(args["pr_number"], str):
        with contextlib.suppress(ValueError):
            args["pr_number"] = int(args["pr_number"])

    # Mode-based tool gating (fail closed before argument parsing)
    mode = tool_context.state.get("agent_mode", Mode.IGNORE)
    if tool.name == "review" and mode != Mode.FORMAL_AUDIT:
        return {
            "error": f"Tool '{tool.name}' is not permitted in mode '{mode}'. "
            f"Only permitted in mode '{Mode.FORMAL_AUDIT}'."
        }
    if tool.name == "resolve_review_thread" and mode != Mode.THREAD_COLLAB:
        return {
            "error": f"Tool '{tool.name}' is not permitted in mode '{mode}'. "
            f"Only permitted in mode '{Mode.THREAD_COLLAB}'."
        }

    # Mandatory Investigation Gate: block review tool if code audit has not used investigative tools yet
    if tool.name == "review" and tool_context.state.get("is_code_audit"):
        investigative_tools = {"read_file", "search_files", "list_dir"}
        tools_executed = tool_context.state.get("tools_executed") or []
        has_investigated = any(t in investigative_tools for t in tools_executed)
        if not has_investigated:
            repo = tool_context.state.get("repo_full_name", "")
            item_id = tool_context.state.get("pr_number") or tool_context.state.get(
                "issue_number", ""
            )
            logger.info(
                "🛑 [Investigation Gate] Blocked premature review() tool call on code PR without prior investigation.",
                extra={
                    "json_fields": {
                        "event_type": "investigation_gate_blocked",
                        "tool": "review",
                        "repo": repo,
                        "item_id": item_id,
                    }
                },
            )
            return {
                "error": (
                    "Mandatory Investigation Gate: You cannot submit a code review before actively exploring the repository. "
                    "Use `search_files`, `list_dir`, or `read_file` to inspect callers, directory layout, contract boundaries, "
                    "and related test suites across the repository before submitting your review."
                )
            }

    # Universal Pydantic input validation gate
    from pydantic import ValidationError

    from webhook_agent.tools.schemas import TOOL_INPUT_SCHEMAS

    schema_cls = TOOL_INPUT_SCHEMAS.get(tool.name)
    if schema_cls is not None:
        try:
            validated = schema_cls.model_validate(args)
            args.clear()
            args.update(validated.model_dump(exclude_unset=False))
        except ValidationError as val_err:
            logger.warning(
                "❌ [Tool Gate Failed] Tool '%s' input validation rejected. Error: %s",
                tool.name,
                val_err,
            )
            return {
                "error": f"Tool '{tool.name}' argument validation failed. Please fix the argument types/names and try again."
            }

    # Track executed tools in session state for downstream audit gates
    tools_executed = tool_context.state.setdefault("tools_executed", [])
    if isinstance(tools_executed, list):
        if tool.name not in tools_executed:
            tools_executed.append(tool.name)
    elif isinstance(tools_executed, set):
        tools_executed.add(tool.name)

    logger.debug("🔧 [Tool Executing] %s (args=%s)", tool.name, _format_tool_args_for_logging(args))
    return None


async def on_tool_error_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception
) -> dict[str, Any] | None:
    """Self-healing error recovery callback."""
    logger.debug("on_tool_error_callback: tool '%s' raised error: %s", tool.name, error)
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
