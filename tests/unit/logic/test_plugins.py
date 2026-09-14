"""Unit tests for ADK Token Optimization Plugins."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from webhook_agent.logic.plugins import (
    PRUNE_MARKER,
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_webhook_history_pruning_under_limit() -> None:
    plugin = WebhookHistoryPruningPlugin(max_events=5)
    ctx = MagicMock()
    req = MagicMock()
    req.contents = ["event0", "event1", "event2"]

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert req.contents == ["event0", "event1", "event2"]


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_webhook_history_pruning_exceeds_limit() -> None:
    plugin = WebhookHistoryPruningPlugin(max_events=4)
    ctx = MagicMock()
    req = MagicMock()
    req.contents = [
        "turn0_system",
        "turn1",
        "turn2",
        "turn3",
        "turn4",
        "turn5",
    ]

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert req.contents == [
        "turn0_system",
        "turn3",
        "turn4",
        "turn5",
    ]
    assert len(req.contents) == 4


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_webhook_history_pruning_no_contents() -> None:
    plugin = WebhookHistoryPruningPlugin(max_events=4)
    ctx = MagicMock()
    req = MagicMock(spec=[])

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_tool_output_pruning_no_session_events() -> None:
    plugin = ToolOutputPruningPlugin()
    ctx = MagicMock()
    ctx._invocation_context = None
    req = MagicMock()

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None


def _make_user_event(text: str) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.function_response = None
    part.function_call = None
    event = MagicMock()
    event.content.parts = [part]
    return event


def _make_tool_event(tool_name: str, response: Any) -> MagicMock:
    part = MagicMock()
    part.text = None
    part.function_call = None
    part.function_response.name = tool_name
    part.function_response.response = response
    event = MagicMock()
    event.content.parts = [part]
    return event


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_tool_output_pruning_protects_recent_turns() -> None:
    plugin = ToolOutputPruningPlugin(
        protect_recent_turns=2, min_part_tokens=10, min_reclaim_tokens=50
    )

    recent_tool_event = _make_tool_event("read_file", "Z" * 400)
    user_event_1 = _make_user_event("Please check this")
    user_event_2 = _make_user_event("Follow up")

    events = [user_event_1, recent_tool_event, user_event_2]

    ctx = MagicMock()
    ctx._invocation_context.session.events = events
    req = MagicMock()

    await plugin.before_model_callback(callback_context=ctx, llm_request=req)

    part = recent_tool_event.content.parts[0]
    assert part.function_response.response == "Z" * 400


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_tool_output_pruning_zeroes_stale_tool_outputs() -> None:
    plugin = ToolOutputPruningPlugin(
        protect_recent_turns=1, min_part_tokens=10, min_reclaim_tokens=50
    )

    stale_tool_event = _make_tool_event("read_file", "X" * 1000)
    user_turn_old = _make_user_event("Inspect files")
    user_turn_recent = _make_user_event("Review complete?")
    recent_tool_event = _make_tool_event("post_review", "OK")

    events = [stale_tool_event, user_turn_old, user_turn_recent, recent_tool_event]

    ctx = MagicMock()
    ctx._invocation_context.session.events = events
    req = MagicMock()

    await plugin.before_model_callback(callback_context=ctx, llm_request=req)

    stale_part = stale_tool_event.content.parts[0]
    assert stale_part.function_response.response == {"pruned": PRUNE_MARKER}

    recent_part = recent_tool_event.content.parts[0]
    assert recent_part.function_response.response == "OK"


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_tool_output_pruning_skips_already_pruned() -> None:
    plugin = ToolOutputPruningPlugin(
        protect_recent_turns=1, min_part_tokens=10, min_reclaim_tokens=50
    )

    already_pruned_event = _make_tool_event("read_file", {"pruned": PRUNE_MARKER})
    user_turn_1 = _make_user_event("First")
    user_turn_2 = _make_user_event("Second")

    events = [already_pruned_event, user_turn_1, user_turn_2]

    ctx = MagicMock()
    ctx._invocation_context.session.events = events
    req = MagicMock()

    await plugin.before_model_callback(callback_context=ctx, llm_request=req)

    part = already_pruned_event.content.parts[0]
    assert part.function_response.response == {"pruned": PRUNE_MARKER}
