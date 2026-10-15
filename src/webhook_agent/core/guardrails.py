"""ADK Guardrails and Graceful Handoff Plugins for Webhook Agent.

Adapted from Google's adk-recipes/long-horizon-harness patterns:
- IterationBudgetPlugin: Enforces maximum turn limits and tool execution budgets.
- GuardrailsPlugin: Detects repeated tool failures and zero-progress loops, triggering graceful handoff.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins import BasePlugin
from google.adk.tools import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger("webhook_agent.core.guardrails")

GUARDRAIL_STATE_KEYS = (
    "guardrail_turn_count",
    "guardrail_tool_calls_count",
    "halt_reason",
    "guardrail_tool_call_history",
    "guardrail_tool_failures",
)


def reset_guardrail_state(state: Any, keys: tuple[str, ...] | list[str] | None = None) -> None:
    """Safely reset guardrail budget and failure tracking keys from session state."""
    target_keys = keys or GUARDRAIL_STATE_KEYS
    for k in target_keys:
        if isinstance(state, dict):
            state.pop(k, None)
            continue
        # ADK State class stores keys in private _value and _delta dicts without a pop/del method
        val = getattr(state, "_value", None)
        if isinstance(val, dict):
            val.pop(k, None)
        delta = getattr(state, "_delta", None)
        if isinstance(delta, dict):
            delta.pop(k, None)
        if hasattr(state, "pop") and callable(state.pop):
            state.pop(k, None)


class IterationBudgetPlugin(BasePlugin):
    """Enforces turn and tool call budgets across model turns in a single review session."""

    def __init__(
        self,
        max_turns: int = 6,
        max_tool_calls: int = 15,
        name: str = "iteration_budget_plugin",
    ) -> None:
        super().__init__(name=name)
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any | None:
        reset_guardrail_state(
            callback_context.state,
            ("guardrail_turn_count", "guardrail_tool_calls_count", "halt_reason"),
        )
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        state = callback_context.state
        turn_count = (state.get("guardrail_turn_count") or 0) + 1
        state["guardrail_turn_count"] = turn_count

        if turn_count > self.max_turns and not state.get("halt_reason"):
            state["halt_reason"] = f"turn_budget_exceeded:{self.max_turns}"
            logger.warning(
                "🛑 [IterationBudgetPlugin] Turn budget of %d exceeded on turn %d",
                self.max_turns,
                turn_count,
            )

        # If halt reason is set, perform graceful handoff: strip tools and request final summary
        halt_reason = state.get("halt_reason")
        if halt_reason:
            if hasattr(llm_request, "tools_dict") and isinstance(llm_request.tools_dict, dict):
                llm_request.tools_dict.clear()

            notice = (
                f"\n\n[⚠️ SYSTEM NOTICE: Execution halted due to '{halt_reason}'. "
                "Do NOT call any further tools. Output your final structured review verdict immediately.]"
            )
            if hasattr(llm_request, "append_instructions"):
                llm_request.append_instructions([notice])

        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        state = tool_context.state
        calls_count = (state.get("guardrail_tool_calls_count") or 0) + 1
        state["guardrail_tool_calls_count"] = calls_count

        if calls_count > self.max_tool_calls:
            state["halt_reason"] = f"tool_budget_exceeded:{self.max_tool_calls}"
            logger.warning(
                "🛑 [IterationBudgetPlugin] Tool call budget of %d exceeded on call %d for '%s'",
                self.max_tool_calls,
                calls_count,
                getattr(tool, "name", "tool"),
            )
            return {
                "error": f"Tool execution halted: tool call budget of {self.max_tool_calls} exceeded. "
                "Please output your formal review JSON immediately."
            }

        if state.get("halt_reason"):
            return {
                "error": f"Tool execution halted: {state['halt_reason']}. "
                "Please output your formal review JSON immediately."
            }

        return None


class GuardrailsPlugin(BasePlugin):
    """Detects repeated tool failures and zero-progress loops, triggering graceful handoff."""

    def __init__(
        self,
        failure_threshold: int = 3,
        no_progress_window: int = 3,
        name: str = "guardrails_plugin",
    ) -> None:
        super().__init__(name=name)
        self.failure_threshold = failure_threshold
        self.no_progress_window = no_progress_window

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any | None:
        reset_guardrail_state(
            callback_context.state,
            ("guardrail_tool_call_history", "guardrail_tool_failures", "halt_reason"),
        )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        state = tool_context.state
        tool_name = getattr(tool, "name", "unknown_tool")

        # Check for repetitive identical tool calls (NoProgressGuard)
        history: list[str] = state.setdefault("guardrail_tool_call_history", [])
        try:
            call_repr = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        except Exception:
            call_repr = f"{tool_name}:{tool_args!s}"
        call_hash = hashlib.sha256(call_repr.encode()).hexdigest()[:12]
        history.append(call_hash)

        # Check if the last N calls are identical
        if len(history) >= self.no_progress_window:
            recent = history[-self.no_progress_window :]
            if len(set(recent)) == 1:
                state["halt_reason"] = f"no_progress_loop:{tool_name}"
                logger.warning(
                    "🛑 [GuardrailsPlugin] Detected %d identical calls to '%s'. Halting loop.",
                    self.no_progress_window,
                    tool_name,
                )
                return {
                    "error": f"No-progress guard triggered: tool '{tool_name}' was called {self.no_progress_window} times with identical arguments. "
                    "Please proceed to your final review verdict."
                }

        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        state = tool_context.state
        tool_name = getattr(tool, "name", "unknown_tool")

        # Determine if tool result indicates an error
        is_error = False
        if isinstance(result, dict):
            if "error" in result:
                is_error = True
            elif "content" in result and isinstance(result["content"], str):
                content_str = result["content"]
                if content_str.startswith(("Error:", "Traceback")):
                    is_error = True
        elif isinstance(result, str) and result.startswith(("Error:", "Traceback")):
            is_error = True

        failures: dict[str, int] = state.setdefault("guardrail_tool_failures", {})
        if is_error:
            failures[tool_name] = failures.get(tool_name, 0) + 1
            if failures[tool_name] >= self.failure_threshold:
                state["halt_reason"] = f"repeated_failure:{tool_name}"
                logger.warning(
                    "🛑 [GuardrailsPlugin] Tool '%s' failed %d consecutive times. Halting execution.",
                    tool_name,
                    failures[tool_name],
                )
        else:
            failures[tool_name] = 0

        return None
