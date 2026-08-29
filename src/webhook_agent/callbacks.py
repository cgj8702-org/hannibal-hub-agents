"""ADK Callbacks Suite for Webhook Agent.

Provides native ADK lifecycle callbacks for:
- Pre-flight free count_tokens API metering, Free Tier TPM chunking (<15k), and rate limit waiting (before_model_callback).
- Post-call token usage auditing (after_model_callback).
- State pre-population with PR metadata and active tier (before_agent_callback).
- Tool parameter validation & sanitization (before_tool_callback).
- Self-healing error recovery (on_tool_error_callback).
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import BaseTool, ToolContext

try:
    from logic.rate_limiter import _resolve_tier, get_active_api_key, rpm_waiter
except ImportError:
    from src.logic.rate_limiter import _resolve_tier, get_active_api_key, rpm_waiter

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


def _check_pr_closed_short_circuit(state: dict[str, Any]) -> None:
    """Check if target PR is registered as closed/merged and abort agent turn immediately."""
    repo_full_name = state.get("repo_full_name") or ""
    pr_number = state.get("pr_number") or state.get("issue_number")
    if repo_full_name and pr_number:
        try:
            from .cancellation import AbortAgentExecution, pr_closed_registry

            if pr_closed_registry.is_closed(str(repo_full_name), int(pr_number)):
                logger.warning(
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
    agent_name = getattr(
        getattr(callback_context, "agent", None), "name", "unknown_agent"
    )
    logger.info("🤖 [SubAgent: %s] Starting sub-agent execution...", agent_name)
    logger.debug(
        "before_agent_callback: initialized active_tier=%s in state for sub-agent '%s'",
        active_tier,
        agent_name,
    )


MODEL_FREE_TPM_LIMITS = {
    "gemma": 15000,
    "gemini": 1000000,
}


def get_model_tpm_limit(model_name: str, tier: str = "free") -> int:
    """Return max TPM limit based on specific model and active tier."""
    model_lower = model_name.lower()
    if tier == "free":
        for family, limit in MODEL_FREE_TPM_LIMITS.items():
            if family in model_lower:
                return limit
        return 15000 if "gemma" in model_lower else 1000000
    return 4000000


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
        default_model = "gemini-3.6-flash"
    target_model = getattr(llm_request, "model", None) or default_model

    input_text = ""
    if hasattr(llm_request, "contents") and llm_request.contents:
        input_text = str(llm_request.contents)

    exact_tokens = len(input_text) // 4 + 500
    if exact_tokens > 0 and api_key:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            resp = client.models.count_tokens(model=target_model, contents=input_text)
            if resp and resp.total_tokens:
                exact_tokens = int(resp.total_tokens)
        except Exception:
            pass

    target_model = getattr(llm_request, "model", None) or "gemini-2.5-flash"
    await rpm_waiter.check_and_wait(
        model=target_model,
        estimated_tokens=exact_tokens,
        tier=active_tier,
    )

    callback_context.state["prompt_tokens"] = exact_tokens
    return None


async def after_model_callback(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Record token usage metadata and sanitize hallucinated tool prefixes after Gemini responds."""
    if hasattr(llm_response, "usage_metadata") and llm_response.usage_metadata:
        total_tokens = getattr(
            llm_response.usage_metadata, "total_token_count", 0
        ) or getattr(llm_response.usage_metadata, "total_tokens", 0)
        callback_context.state["total_tokens"] = total_tokens
        logger.debug("after_model_callback: recorded total_tokens=%d", total_tokens)

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
        try:
            args["pr_number"] = int(args["pr_number"])
        except ValueError:
            pass

    return None


async def on_tool_error_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception
) -> dict[str, Any] | None:
    """Self-healing error recovery callback."""
    logger.warning(
        "on_tool_error_callback: tool '%s' raised error: %s", tool.name, error
    )
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
