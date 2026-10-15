"""Unit tests for ADK Guardrails and IterationBudget plugins."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from webhook_agent.core.guardrails import GuardrailsPlugin, IterationBudgetPlugin

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.anyio
async def test_iteration_budget_turn_under_limit() -> None:
    plugin = IterationBudgetPlugin(max_turns=3, max_tool_calls=10)
    ctx = MagicMock()
    ctx.state = {}
    req = MagicMock()
    req.tools_dict = {"tool1": MagicMock()}

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert ctx.state["guardrail_turn_count"] == 1
    assert "halt_reason" not in ctx.state
    assert len(req.tools_dict) == 1


@pytest.mark.anyio
async def test_iteration_budget_turn_exceeded_clears_tools_and_appends_instructions() -> None:
    plugin = IterationBudgetPlugin(max_turns=2, max_tool_calls=10)
    ctx = MagicMock()
    ctx.state = {"guardrail_turn_count": 2}
    req = MagicMock()
    req.tools_dict = {"search_files": MagicMock()}
    req.append_instructions = MagicMock()

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert ctx.state["guardrail_turn_count"] == 3
    assert ctx.state["halt_reason"] == "turn_budget_exceeded:2"
    assert len(req.tools_dict) == 0
    req.append_instructions.assert_called_once()
    appended_text = req.append_instructions.call_args[0][0][0]
    assert "[⚠️ SYSTEM NOTICE: Execution halted" in appended_text


@pytest.mark.anyio
async def test_iteration_budget_tool_call_budget_exceeded() -> None:
    plugin = IterationBudgetPlugin(max_turns=5, max_tool_calls=2)
    ctx = MagicMock()
    ctx.state = {"guardrail_tool_calls_count": 2}
    tool = MagicMock()
    tool.name = "search_files"

    result = await plugin.before_tool_callback(
        tool=tool,
        tool_args={"query": "test"},
        tool_context=ctx,
    )
    assert isinstance(result, dict)
    assert "error" in result
    assert "tool call budget of 2 exceeded" in result["error"]
    assert ctx.state["halt_reason"] == "tool_budget_exceeded:2"


@pytest.mark.anyio
async def test_iteration_budget_halt_reason_blocks_subsequent_tool() -> None:
    plugin = IterationBudgetPlugin(max_turns=5, max_tool_calls=10)
    ctx = MagicMock()
    ctx.state = {"guardrail_tool_calls_count": 1, "halt_reason": "turn_budget_exceeded:5"}
    tool = MagicMock()
    tool.name = "search_files"

    result = await plugin.before_tool_callback(
        tool=tool,
        tool_args={"query": "test"},
        tool_context=ctx,
    )
    assert isinstance(result, dict)
    assert "error" in result
    assert "turn_budget_exceeded:5" in result["error"]


@pytest.mark.anyio
async def test_guardrails_no_progress_loop() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=3)
    ctx = MagicMock()
    ctx.state = {}
    tool = MagicMock()
    tool.name = "search_files"
    args = {"query": "import asyncio"}

    # Call 1 & 2: allowed
    res1 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx)
    assert res1 is None
    res2 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx)
    assert res2 is None

    # Call 3: identical -> triggers no-progress guard
    res3 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx)
    assert isinstance(res3, dict)
    assert "No-progress guard triggered" in res3["error"]
    assert ctx.state["halt_reason"] == "no_progress_loop:search_files"


@pytest.mark.anyio
async def test_guardrails_repeated_failure() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=5)
    ctx = MagicMock()
    ctx.state = {}
    tool = MagicMock()
    tool.name = "flaky_tool"

    # Tool error 1
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"error": "FileNotFoundError"},
    )
    assert "halt_reason" not in ctx.state
    assert ctx.state["guardrail_tool_failures"]["flaky_tool"] == 1

    # Tool error 2 (nested content str formatted error)
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "Error: Permission denied"},
    )
    assert "halt_reason" not in ctx.state
    assert ctx.state["guardrail_tool_failures"]["flaky_tool"] == 2

    # Tool error 3 -> reaches failure_threshold
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "Traceback (most recent call last): ..."},
    )
    assert ctx.state["halt_reason"] == "repeated_failure:flaky_tool"


@pytest.mark.anyio
async def test_guardrails_failure_resets_on_success() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=5)
    ctx = MagicMock()
    ctx.state = {}
    tool = MagicMock()
    tool.name = "flaky_tool"

    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"error": "fail 1"},
    )
    assert ctx.state["guardrail_tool_failures"]["flaky_tool"] == 1

    # Success resets counter
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "all good"},
    )
    assert ctx.state["guardrail_tool_failures"]["flaky_tool"] == 0
    assert "halt_reason" not in ctx.state


@pytest.mark.anyio
async def test_iteration_budget_before_agent_callback_resets_budget_keys() -> None:
    plugin = IterationBudgetPlugin(max_turns=3, max_tool_calls=5)
    ctx = MagicMock()
    ctx.state = {
        "guardrail_turn_count": 3,
        "guardrail_tool_calls_count": 5,
        "halt_reason": "turn_budget_exceeded:3",
        "custom_key": "preserve_me",
    }
    agent = MagicMock()

    await plugin.before_agent_callback(agent=agent, callback_context=ctx)

    assert "guardrail_turn_count" not in ctx.state
    assert "guardrail_tool_calls_count" not in ctx.state
    assert "halt_reason" not in ctx.state
    assert ctx.state["custom_key"] == "preserve_me"


@pytest.mark.anyio
async def test_guardrails_before_agent_callback_resets_history_and_failures() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=3)
    ctx = MagicMock()
    ctx.state = {
        "guardrail_tool_call_history": ["hash1", "hash2"],
        "guardrail_tool_failures": {"search_files": 2},
        "halt_reason": "no_progress_loop:search_files",
        "custom_key": "preserve_me",
    }
    agent = MagicMock()

    await plugin.before_agent_callback(agent=agent, callback_context=ctx)

    assert "guardrail_tool_call_history" not in ctx.state
    assert "guardrail_tool_failures" not in ctx.state
    assert "halt_reason" not in ctx.state
    assert ctx.state["custom_key"] == "preserve_me"


@pytest.mark.anyio
async def test_session_state_leak_prevention_across_deliveries() -> None:
    """Verify that a session halted in delivery 1 does not strip tools in delivery 2."""
    from webhook_agent.core.callbacks import before_agent_callback

    shared_session_state: dict[str, Any] = {}

    # Delivery 1: hits turn limit
    budget_plugin = IterationBudgetPlugin(max_turns=2, max_tool_calls=5)
    guard_plugin = GuardrailsPlugin(failure_threshold=2, no_progress_window=2)

    ctx1 = MagicMock()
    ctx1.state = shared_session_state
    req1 = MagicMock()
    req1.tools_dict = {"search_files": MagicMock()}

    # Run turn 1 and turn 2
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)
    # Turn 3 exceeds budget of 2
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)

    assert shared_session_state["halt_reason"] == "turn_budget_exceeded:2"
    assert len(req1.tools_dict) == 0

    # Delivery 2 on the same PR / session ID:
    ctx2 = MagicMock()
    ctx2.state = shared_session_state
    ctx2.agent_name = "code_auditor"
    agent2 = MagicMock()

    # Plugins and callbacks run at the start of Delivery 2
    await budget_plugin.before_agent_callback(agent=agent2, callback_context=ctx2)
    await guard_plugin.before_agent_callback(agent=agent2, callback_context=ctx2)
    await before_agent_callback(callback_context=ctx2)

    # State must be completely clean of halt_reason and turn counts
    assert "halt_reason" not in shared_session_state
    assert "guardrail_turn_count" not in shared_session_state
    assert "guardrail_tool_calls_count" not in shared_session_state

    # Delivery 2 model request has tools intact and proceeds
    req2 = MagicMock()
    req2.tools_dict = {"read_file": MagicMock()}
    await budget_plugin.before_model_callback(callback_context=ctx2, llm_request=req2)
    assert len(req2.tools_dict) == 1
    assert shared_session_state["guardrail_turn_count"] == 1
    assert "halt_reason" not in shared_session_state
