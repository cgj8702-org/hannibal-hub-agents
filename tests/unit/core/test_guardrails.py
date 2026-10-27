"""Unit tests for ADK Guardrails and IterationBudget plugins."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from webhook_agent.core.guardrails import (
    GUARDRAIL_STATE_KEYS,
    GuardrailsPlugin,
    IterationBudgetPlugin,
    reset_guardrail_state,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


@pytest.mark.anyio
async def test_iteration_budget_turn_under_limit() -> None:
    plugin = IterationBudgetPlugin(max_turns=3, max_tool_calls=10)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
    ctx.state = {}
    req = MagicMock()
    req.tools_dict = {"tool1": MagicMock()}

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert ctx.state["guardrail_run"]["turns"] == 1
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert len(req.tools_dict) == 1


@pytest.mark.anyio
async def test_iteration_budget_turn_exceeded_clears_tools_and_appends_instructions() -> None:
    plugin = IterationBudgetPlugin(max_turns=2, max_tool_calls=10)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
    ctx.state = {
        "guardrail_run": {
            "invocation_id": "inv_1",
            "turns": 2,
            "tool_calls": 0,
            "halt_reason": None,
            "history": [],
            "failures": {},
        }
    }
    req = MagicMock()
    req.tools_dict = {"search_files": MagicMock()}
    req.append_instructions = MagicMock()

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert ctx.state["guardrail_run"]["turns"] == 3
    assert ctx.state["guardrail_run"]["halt_reason"] == "turn_budget_exceeded:2"
    assert len(req.tools_dict) == 0
    req.append_instructions.assert_called_once()
    appended_text = req.append_instructions.call_args[0][0][0]
    assert "[⚠️ SYSTEM NOTICE: Execution halted" in appended_text
    assert "concise, helpful summary response" in appended_text

    # Verify that in FORMAL_AUDIT mode it asks for structured review verdict
    from webhook_agent.core.policy import Mode

    ctx.state["agent_mode"] = Mode.FORMAL_AUDIT
    req.reset_mock()
    res2 = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res2 is None
    req.append_instructions.assert_called_once()
    appended_text2 = req.append_instructions.call_args[0][0][0]
    assert "Output your final structured review verdict immediately" in appended_text2


@pytest.mark.anyio
async def test_iteration_budget_conversational_mode_effective_max_turns() -> None:
    from webhook_agent.core.policy import Mode

    # In conversational mode, even if plugin max_turns is 10, effective budget is 5
    plugin = IterationBudgetPlugin(max_turns=10, max_tool_calls=25)
    ctx = MagicMock()
    ctx.invocation_id = "inv_conv"
    ctx.state = {
        "agent_mode": Mode.GENERAL_PAIR_PROGRAMMING,
        "guardrail_run": {
            "invocation_id": "inv_conv",
            "turns": 5,
            "tool_calls": 0,
            "halt_reason": None,
            "history": [],
            "failures": {},
        },
    }
    req = MagicMock()
    req.tools_dict = {"search_files": MagicMock()}
    req.append_instructions = MagicMock()

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert ctx.state["guardrail_run"]["turns"] == 6
    assert ctx.state["guardrail_run"]["halt_reason"] == "turn_budget_exceeded:5"
    assert len(req.tools_dict) == 0


@pytest.mark.anyio
async def test_iteration_budget_tool_call_budget_exceeded() -> None:
    plugin = IterationBudgetPlugin(max_turns=5, max_tool_calls=2)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
    ctx.state = {
        "guardrail_run": {
            "invocation_id": "inv_1",
            "turns": 0,
            "tool_calls": 2,
            "halt_reason": None,
            "history": [],
            "failures": {},
        }
    }
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
    assert ctx.state["guardrail_run"]["halt_reason"] == "tool_budget_exceeded:2"


@pytest.mark.anyio
async def test_iteration_budget_mode_aware_tool_budgets() -> None:
    from webhook_agent.core.policy import Mode

    plugin = IterationBudgetPlugin(max_turns=10, max_tool_calls=25)

    # 1. ISSUE_TRIAGE capped at 2 tool calls
    ctx = MagicMock()
    ctx.invocation_id = "inv_triage"
    ctx.state = {
        "agent_mode": Mode.ISSUE_TRIAGE,
        "guardrail_run": {
            "invocation_id": "inv_triage",
            "turns": 1,
            "tool_calls": 2,
            "halt_reason": None,
            "history": [],
            "failures": {},
        },
    }
    tool = MagicMock()
    tool.name = "search_files"

    result = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    assert isinstance(result, dict)
    assert "tool call budget of 2 exceeded" in result["error"]
    assert "concise, helpful summary response" in result["error"]
    assert ctx.state["guardrail_run"]["halt_reason"] == "tool_budget_exceeded:2"

    # 2. GENERAL_PAIR_PROGRAMMING capped at 4 tool calls
    ctx_chat = MagicMock()
    ctx_chat.invocation_id = "inv_chat"
    ctx_chat.state = {
        "agent_mode": Mode.GENERAL_PAIR_PROGRAMMING,
        "guardrail_run": {
            "invocation_id": "inv_chat",
            "turns": 1,
            "tool_calls": 4,
            "halt_reason": None,
            "history": [],
            "failures": {},
        },
    }
    result_chat = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx_chat)
    assert isinstance(result_chat, dict)
    assert "tool call budget of 4 exceeded" in result_chat["error"]
    assert ctx_chat.state["guardrail_run"]["halt_reason"] == "tool_budget_exceeded:4"

    # 3. FORMAL_AUDIT allows up to max_tool_calls (25)
    ctx_audit = MagicMock()
    ctx_audit.invocation_id = "inv_audit"
    ctx_audit.state = {
        "agent_mode": Mode.FORMAL_AUDIT,
        "guardrail_run": {
            "invocation_id": "inv_audit",
            "turns": 1,
            "tool_calls": 4,
            "halt_reason": None,
            "history": [],
            "failures": {},
        },
    }
    result_audit = await plugin.before_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx_audit
    )
    assert result_audit is None  # Allowed! Call 5 is well within 25.


@pytest.mark.anyio
async def test_iteration_budget_halt_reason_blocks_subsequent_tool() -> None:
    plugin = IterationBudgetPlugin(max_turns=5, max_tool_calls=10)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
    ctx.state = {
        "guardrail_run": {
            "invocation_id": "inv_1",
            "turns": 0,
            "tool_calls": 1,
            "halt_reason": "turn_budget_exceeded:5",
            "history": [],
            "failures": {},
        }
    }
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
    ctx.invocation_id = "inv_1"
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
    assert ctx.state["guardrail_run"]["halt_reason"] == "no_progress_loop:search_files"


@pytest.mark.anyio
async def test_guardrails_repeated_failure() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=5)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
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
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert ctx.state["guardrail_run"]["failures"]["flaky_tool"] == 1

    # Tool error 2 (nested content str formatted error)
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "Error: Permission denied"},
    )
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert ctx.state["guardrail_run"]["failures"]["flaky_tool"] == 2

    # Tool error 3 -> reaches failure_threshold
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "Traceback (most recent call last): ..."},
    )
    assert ctx.state["guardrail_run"]["halt_reason"] == "repeated_failure:flaky_tool"


@pytest.mark.anyio
async def test_guardrails_failure_resets_on_success() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=5)
    ctx = MagicMock()
    ctx.invocation_id = "inv_1"
    ctx.state = {}
    tool = MagicMock()
    tool.name = "flaky_tool"

    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"error": "fail 1"},
    )
    assert ctx.state["guardrail_run"]["failures"]["flaky_tool"] == 1

    # Success resets counter
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"content": "all good"},
    )
    assert ctx.state["guardrail_run"]["failures"]["flaky_tool"] == 0
    assert ctx.state["guardrail_run"]["halt_reason"] is None


@pytest.mark.anyio
async def test_iteration_budget_before_agent_callback_resets_budget_keys() -> None:
    plugin = IterationBudgetPlugin(max_turns=3, max_tool_calls=5)
    ctx = MagicMock()
    ctx.invocation_id = "inv_2"
    ctx.state = {
        "guardrail_run": {
            "invocation_id": "inv_1",
            "turns": 3,
            "tool_calls": 5,
            "halt_reason": "turn_budget_exceeded:3",
            "history": [],
            "failures": {},
        },
        "custom_key": "preserve_me",
    }
    agent = MagicMock()

    await plugin.before_agent_callback(agent=agent, callback_context=ctx)

    assert ctx.state["guardrail_run"]["invocation_id"] == "inv_2"
    assert ctx.state["guardrail_run"]["turns"] == 0
    assert ctx.state["guardrail_run"]["tool_calls"] == 0
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert ctx.state["custom_key"] == "preserve_me"


@pytest.mark.anyio
async def test_guardrails_before_agent_callback_resets_history_and_failures() -> None:
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=3)
    ctx = MagicMock()
    ctx.invocation_id = "inv_2"
    ctx.state = {
        "guardrail_run": {
            "invocation_id": "inv_1",
            "turns": 0,
            "tool_calls": 0,
            "halt_reason": "no_progress_loop:search_files",
            "history": ["hash1", "hash2"],
            "failures": {"search_files": 2},
        },
        "custom_key": "preserve_me",
    }
    agent = MagicMock()

    await plugin.before_agent_callback(agent=agent, callback_context=ctx)

    assert ctx.state["guardrail_run"]["invocation_id"] == "inv_2"
    assert ctx.state["guardrail_run"]["history"] == []
    assert ctx.state["guardrail_run"]["failures"] == {}
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert ctx.state["custom_key"] == "preserve_me"


@pytest.mark.anyio
async def test_reset_guardrail_state_clears_keys() -> None:
    state = {
        "guardrail_run": {"invocation_id": "inv_1", "turns": 2},
        "guardrail_turn_count": 5,
        "halt_reason": "turn_budget_exceeded:3",
        "preserve_key": "ok",
    }
    reset_guardrail_state(state)
    for k in GUARDRAIL_STATE_KEYS:
        assert k not in state
    assert state["preserve_key"] == "ok"


@pytest.mark.anyio
async def test_session_state_leak_prevention_across_deliveries() -> None:
    """Verify that a session halted in delivery 1 does not strip tools in delivery 2."""
    from webhook_agent.core.callbacks import before_agent_callback

    shared_session_state: dict[str, Any] = {}

    # Delivery 1: hits turn limit
    budget_plugin = IterationBudgetPlugin(max_turns=2, max_tool_calls=5)
    guard_plugin = GuardrailsPlugin(failure_threshold=2, no_progress_window=2)

    ctx1 = MagicMock()
    ctx1.invocation_id = "delivery_1_inv"
    ctx1.state = shared_session_state
    req1 = MagicMock()
    req1.tools_dict = {"search_files": MagicMock()}

    # Run turn 1 and turn 2
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)
    # Turn 3 exceeds budget of 2
    await budget_plugin.before_model_callback(callback_context=ctx1, llm_request=req1)

    assert shared_session_state["guardrail_run"]["halt_reason"] == "turn_budget_exceeded:2"
    assert len(req1.tools_dict) == 0

    # Delivery 2 on the same PR / session ID:
    ctx2 = MagicMock()
    ctx2.invocation_id = "delivery_2_inv"
    ctx2.state = shared_session_state
    ctx2.agent_name = "code_auditor"
    agent2 = MagicMock()

    # Plugins and callbacks run at the start of Delivery 2
    await budget_plugin.before_agent_callback(agent=agent2, callback_context=ctx2)
    await guard_plugin.before_agent_callback(agent=agent2, callback_context=ctx2)
    await before_agent_callback(callback_context=ctx2)

    # State must be clean of previous halt_reason
    assert "halt_reason" not in shared_session_state
    # If guardrail_run was reset, it will be None or absent until the first model turn
    assert shared_session_state.get("guardrail_run") is None

    # Delivery 2 model request has tools intact and proceeds
    req2 = MagicMock()
    req2.tools_dict = {"read_file": MagicMock()}
    await budget_plugin.before_model_callback(callback_context=ctx2, llm_request=req2)
    assert len(req2.tools_dict) == 1
    assert shared_session_state["guardrail_run"]["invocation_id"] == "delivery_2_inv"
    assert shared_session_state["guardrail_run"]["turns"] == 1
    assert shared_session_state["guardrail_run"]["halt_reason"] is None


class _StubLlm(BaseLlm):
    """Minimal stub model yielding static text without network calls."""

    def __init__(self, model: str = "stub-model") -> None:
        super().__init__(model=model)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        content = types.Content(
            role="model",
            parts=[types.Part.from_text(text="ok")],
        )
        yield LlmResponse(content=content)


@pytest.mark.anyio
async def test_multi_invocation_stub_model_budget_isolation() -> None:
    """Run 5 invocations on one session; assert halt_reason stays unset after every invocation."""
    agent = LlmAgent(name="test_agent", model=_StubLlm())
    plugin = IterationBudgetPlugin(max_turns=3)
    app = App(name="test_app", root_agent=agent, plugins=[plugin])
    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)

    session = await session_service.create_session(
        app_name="test_app",
        user_id="test_user",
    )

    for i in range(5):
        msg = types.Content(role="user", parts=[types.Part.from_text(text=f"hello {i}")])
        async for _ in runner.run_async(
            session_id=session.id,
            user_id="test_user",
            new_message=msg,
        ):
            pass

        sess = await session_service.get_session(
            app_name="test_app", user_id="test_user", session_id=session.id
        )
        assert sess is not None
        run_state = sess.state.get("guardrail_run")
        assert run_state is not None, f"Invocation {i} missing guardrail_run"
        assert run_state.get("turns") == 1
        assert run_state.get("halt_reason") is None


@pytest.mark.anyio
async def test_single_invocation_exceeding_max_turns_halts() -> None:
    """Verify that a single invocation exceeding max_turns halts properly."""
    plugin = IterationBudgetPlugin(max_turns=3, max_tool_calls=10)
    ctx = MagicMock()
    ctx.invocation_id = "inv_single"
    ctx.state = {}

    req1 = MagicMock()
    req1.tools_dict = {"tool_a": MagicMock()}
    await plugin.before_model_callback(callback_context=ctx, llm_request=req1)
    assert ctx.state["guardrail_run"]["turns"] == 1
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert len(req1.tools_dict) == 1

    req2 = MagicMock()
    req2.tools_dict = {"tool_a": MagicMock()}
    await plugin.before_model_callback(callback_context=ctx, llm_request=req2)
    assert ctx.state["guardrail_run"]["turns"] == 2
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert len(req2.tools_dict) == 1

    req3 = MagicMock()
    req3.tools_dict = {"tool_a": MagicMock()}
    await plugin.before_model_callback(callback_context=ctx, llm_request=req3)
    assert ctx.state["guardrail_run"]["turns"] == 3
    assert ctx.state["guardrail_run"]["halt_reason"] is None
    assert len(req3.tools_dict) == 1

    # Turn 4: exceeds max_turns=3
    req4 = MagicMock()
    req4.tools_dict = {"tool_a": MagicMock()}
    req4.append_instructions = MagicMock()
    await plugin.before_model_callback(callback_context=ctx, llm_request=req4)
    assert ctx.state["guardrail_run"]["turns"] == 4
    assert ctx.state["guardrail_run"]["halt_reason"] == "turn_budget_exceeded:3"
    assert len(req4.tools_dict) == 0
    req4.append_instructions.assert_called_once()


@pytest.mark.anyio
async def test_no_progress_detection_does_not_trigger_across_invocations() -> None:
    """Verify that identical tool calls across distinct invocations do not trigger loop guard."""
    plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=3)
    shared_session_state: dict[str, Any] = {}
    tool = MagicMock()
    tool.name = "search_files"
    args = {"query": "def calculate():"}

    # Invocation 1: 2 identical calls (not yet triggering window of 3)
    ctx1 = MagicMock()
    ctx1.invocation_id = "inv_1"
    ctx1.state = shared_session_state
    res1 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx1)
    assert res1 is None
    res2 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx1)
    assert res2 is None
    assert len(shared_session_state["guardrail_run"]["history"]) == 2
    assert shared_session_state["guardrail_run"]["halt_reason"] is None

    # Invocation 2: 1 identical call (would be 3rd total, but is 1st in this invocation)
    ctx2 = MagicMock()
    ctx2.invocation_id = "inv_2"
    ctx2.state = shared_session_state
    res3 = await plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx2)
    assert res3 is None
    # History for inv_2 must only have length 1
    assert len(shared_session_state["guardrail_run"]["history"]) == 1
    assert shared_session_state["guardrail_run"]["halt_reason"] is None


@pytest.mark.anyio
async def test_plugins_share_same_guardrail_run_state() -> None:
    """Verify that halt_reason set by GuardrailsPlugin is visible to IterationBudgetPlugin."""
    budget_plugin = IterationBudgetPlugin(max_turns=5, max_tool_calls=10)
    guard_plugin = GuardrailsPlugin(failure_threshold=2, no_progress_window=2)

    ctx = MagicMock()
    ctx.invocation_id = "inv_shared"
    ctx.state = {}
    tool = MagicMock()
    tool.name = "repeated_failing_tool"

    # Trip guardrails failure threshold
    await guard_plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx, result={"error": "fail 1"}
    )
    await guard_plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx, result={"error": "fail 2"}
    )
    assert ctx.state["guardrail_run"]["halt_reason"] == "repeated_failure:repeated_failing_tool"

    # IterationBudgetPlugin sees halt_reason and halts
    res = await budget_plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    assert isinstance(res, dict)
    assert "error" in res
    assert "repeated_failure:repeated_failing_tool" in res["error"]
