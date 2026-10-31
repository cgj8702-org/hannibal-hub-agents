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
    "guardrail_run",
)


def _run_state(state: Any, invocation_id: str | None) -> dict[str, Any]:
    """Retrieve or initialize the guardrail run state dict scoped to invocation_id."""
    existing = state.get("guardrail_run") if hasattr(state, "get") else None
    if (
        isinstance(existing, dict)
        and invocation_id is not None
        and existing.get("invocation_id") == invocation_id
    ):
        return existing
    if (
        isinstance(existing, dict)
        and invocation_id is None
        and existing.get("invocation_id") is None
        and "invocation_id" in existing
    ):
        return existing

    fresh: dict[str, Any] = {
        "invocation_id": invocation_id,
        "turns": 0,
        "tool_calls": 0,
        "halt_reason": None,
        "history": [],
        "failures": {},
    }
    state["guardrail_run"] = fresh
    return fresh


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
        max_turns: int = 8,
        max_tool_calls: int = 30,
        name: str = "iteration_budget_plugin",
    ) -> None:
        super().__init__(name=name)
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any | None:
        inv_id = getattr(callback_context, "invocation_id", None)
        _run_state(callback_context.state, inv_id)
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        state = callback_context.state
        inv_id = getattr(callback_context, "invocation_id", None)
        run = _run_state(state, inv_id)
        run["turns"] = run.get("turns", 0) + 1
        state["guardrail_run"] = run

        # Apply tighter turn budget for conversational / triage modes (default 7 turns)
        # while preserving full max_turns (8) for formal audit reviews.
        mode = state.get("agent_mode")
        from webhook_agent.core.policy import Mode

        effective_max_turns = (
            self.max_turns if mode == Mode.FORMAL_AUDIT or mode is None else min(7, self.max_turns)
        )

        if run["turns"] > effective_max_turns and not run.get("halt_reason"):
            run["halt_reason"] = f"turn_budget_exceeded:{effective_max_turns}"
            state["guardrail_run"] = run
            logger.warning(
                "🛑 [IterationBudgetPlugin] Turn budget of %d exceeded on turn %d (mode=%s)",
                effective_max_turns,
                run["turns"],
                mode,
            )

        # If halt reason is set, perform graceful handoff: strip tools and request final summary
        halt_reason = run.get("halt_reason")
        if halt_reason:
            if hasattr(llm_request, "tools_dict") and isinstance(llm_request.tools_dict, dict):
                llm_request.tools_dict.clear()

            mode = state.get("agent_mode")
            from webhook_agent.core.policy import Mode

            if mode == Mode.FORMAL_AUDIT:
                notice = (
                    f"\n\n[⚠️ SYSTEM NOTICE: Execution halted due to '{halt_reason}'. "
                    "Do NOT call any further tools. Output your final structured review verdict immediately.]"
                )
            else:
                notice = (
                    f"\n\n[⚠️ SYSTEM NOTICE: Execution halted due to '{halt_reason}'. "
                    "Do NOT call any further tools. Provide a concise, helpful summary response to the user immediately.]"
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
        inv_id = getattr(tool_context, "invocation_id", None)
        run = _run_state(state, inv_id)
        run["tool_calls"] = run.get("tool_calls", 0) + 1
        state["guardrail_run"] = run

        # Apply programmatic mode-aware tool call budgets:
        # - FORMAL_AUDIT: retains full max_tool_calls (30) for multi-file AST/diff inspection.
        # - ISSUE_TRIAGE: capped at 8 tool calls.
        # - GENERAL_PAIR_PROGRAMMING / THREAD_COLLAB: capped at 10 tool calls.
        mode = state.get("agent_mode")
        from webhook_agent.core.policy import Mode

        if mode == Mode.ISSUE_TRIAGE:
            effective_max_tools = min(8, self.max_tool_calls)
        elif mode in (Mode.GENERAL_PAIR_PROGRAMMING, Mode.THREAD_COLLAB):
            effective_max_tools = min(10, self.max_tool_calls)
        else:
            effective_max_tools = self.max_tool_calls

        if run["tool_calls"] > effective_max_tools:
            run["halt_reason"] = f"tool_budget_exceeded:{effective_max_tools}"
            state["guardrail_run"] = run
            logger.warning(
                "🛑 [IterationBudgetPlugin] Tool call budget of %d exceeded on call %d for '%s' (mode=%s)",
                effective_max_tools,
                run["tool_calls"],
                getattr(tool, "name", "tool"),
                mode,
            )
            halt_msg = (
                f"Tool execution halted: tool call budget of {effective_max_tools} exceeded. "
                + (
                    "Please output your formal review JSON immediately."
                    if mode == Mode.FORMAL_AUDIT
                    else "Please provide a concise, helpful summary response to the user immediately."
                )
            )
            return {"error": halt_msg}

        if run.get("halt_reason"):
            halt_msg = f"Tool execution halted: {run['halt_reason']}. " + (
                "Please output your formal review JSON immediately."
                if mode == Mode.FORMAL_AUDIT
                else "Please provide a concise, helpful summary response to the user immediately."
            )
            return {"error": halt_msg}

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
        inv_id = getattr(callback_context, "invocation_id", None)
        _run_state(callback_context.state, inv_id)
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        state = tool_context.state
        inv_id = getattr(tool_context, "invocation_id", None)
        run = _run_state(state, inv_id)
        tool_name = getattr(tool, "name", "unknown_tool")

        # Check for repetitive identical tool calls (NoProgressGuard)
        history: list[str] = run.setdefault("history", [])
        try:
            call_repr = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        except Exception:
            call_repr = f"{tool_name}:{tool_args!s}"
        call_hash = hashlib.sha256(call_repr.encode()).hexdigest()[:12]
        history.append(call_hash)
        state["guardrail_run"] = run

        # Check if the last N calls are identical
        if len(history) >= self.no_progress_window:
            recent = history[-self.no_progress_window :]
            if len(set(recent)) == 1:
                run["halt_reason"] = f"no_progress_loop:{tool_name}"
                state["guardrail_run"] = run
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
        inv_id = getattr(tool_context, "invocation_id", None)
        run = _run_state(state, inv_id)
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

        failures: dict[str, int] = run.setdefault("failures", {})
        if is_error:
            failures[tool_name] = failures.get(tool_name, 0) + 1
            if failures[tool_name] >= self.failure_threshold:
                run["halt_reason"] = f"repeated_failure:{tool_name}"
                logger.warning(
                    "🛑 [GuardrailsPlugin] Tool '%s' failed %d consecutive times. Halting execution.",
                    tool_name,
                    failures[tool_name],
                )
        else:
            failures[tool_name] = 0

        state["guardrail_run"] = run
        return None
