"""Unit tests for ADK Callbacks Suite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from webhook_agent.callbacks import (
    after_model_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    get_model_tpm_limit,
    on_tool_error_callback,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_get_model_tpm_limit() -> None:
    assert get_model_tpm_limit("gemma-4-31b-it", "free") == 15000
    assert get_model_tpm_limit("gemini-2.5-flash", "free") == 1000000
    assert get_model_tpm_limit("gemini-2.5-flash", "paid") == 4000000


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_before_agent_callback() -> None:
    ctx = MagicMock()
    ctx.state = {}
    await before_agent_callback(ctx)
    assert "active_tier" in ctx.state
    assert ctx.state["active_tier"] in ("free", "paid")


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_before_model_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = MagicMock()
    ctx.state = {"active_tier": "free"}
    ctx.agent.model = "gemini-2.5-flash"

    req = MagicMock()
    req.contents = "test content"
    req.model = "gemini-2.5-flash"

    res = await before_model_callback(ctx, req)
    assert res is None
    assert "prompt_tokens" in ctx.state


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_after_model_callback() -> None:
    ctx = MagicMock()
    ctx.state = {}

    resp = MagicMock()
    resp.usage_metadata.total_token_count = 125

    res = await after_model_callback(ctx, resp)
    assert res is None
    assert ctx.state.get("total_tokens") == 125


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_before_tool_callback_sanitization() -> None:
    tool = MagicMock()
    args = {"pr_number": "42"}
    ctx = MagicMock()

    res = await before_tool_callback(tool, args, ctx)
    assert res is None
    assert args["pr_number"] == 42


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_on_tool_error_callback() -> None:
    tool = MagicMock()
    tool.name = "update_branch_from_base"
    args = {"pr_number": 63}
    ctx = MagicMock()
    ctx.state = {}

    err = Exception("Merge conflict 422")
    res = await on_tool_error_callback(tool, args, ctx, err)
    assert res is not None
    assert res["success"] is False
    assert ctx.state.get("trigger_worktree_conflict_resolution") is True


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_before_tool_callback_allow_multiple_mutating_tools() -> None:
    tool = MagicMock()
    tool.name = "review"
    args = {}
    ctx = MagicMock()
    ctx.state = {}

    res1 = await before_tool_callback(tool, args, ctx)
    assert res1 is None

    tool2 = MagicMock()
    tool2.name = "add_comment"
    res2 = await before_tool_callback(tool2, args, ctx)
    assert res2 is None


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_on_tool_error_callback_rate_limit() -> None:
    tool = MagicMock()
    tool.name = "read_file"
    args = {"path": "main.py"}
    ctx = MagicMock()

    err = Exception("429 RESOURCE_EXHAUSTED")
    res = await on_tool_error_callback(tool, args, ctx, err)
    assert res is not None
    assert res["success"] is False
    assert "temporary limit or error" in res["detail"]


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_on_tool_error_callback_search_agent() -> None:
    tool = MagicMock()
    tool.name = "search_agent"
    args = {"query": "python docs"}
    ctx = MagicMock()

    err = Exception("Search connection timeout")
    res = await on_tool_error_callback(tool, args, ctx, err)
    assert res is not None
    assert res["success"] is False
    assert "search_agent" in res["detail"]
