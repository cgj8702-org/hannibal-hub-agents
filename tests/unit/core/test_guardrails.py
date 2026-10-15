"""Unit tests for ADK Guardrails and IterationBudget plugins."""

from __future__ import annotations

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
