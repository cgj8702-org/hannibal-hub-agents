"""Unit tests for ADK Callbacks Suite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from webhook_agent.callbacks import (
    after_model_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)


@pytest.mark.anyio
async def test_before_agent_callback() -> None:
    ctx = MagicMock()
    ctx.state = {}
    await before_agent_callback(ctx)
    assert "active_tier" in ctx.state
    assert ctx.state["active_tier"] in ("free", "paid")


@pytest.mark.anyio
async def test_before_model_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_FREE_KEY", "fk_test_123")
    ctx = MagicMock()
    ctx.state = {"active_tier": "free"}
    ctx.agent.model = "gemini-2.5-flash"

    req = MagicMock()
    req.contents = "test content"
    req.model = "gemini-2.5-flash"

    res = await before_model_callback(ctx, req)
    assert res is None
    assert "prompt_tokens" in ctx.state


@pytest.mark.anyio
async def test_after_model_callback() -> None:
    ctx = MagicMock()
    ctx.state = {}

    resp = MagicMock()
    resp.usage_metadata.total_token_count = 125

    res = await after_model_callback(ctx, resp)
    assert res is None
    assert ctx.state.get("total_tokens") == 125


@pytest.mark.anyio
async def test_before_tool_callback_sanitization() -> None:
    tool = MagicMock()
    args = {"pr_number": "42"}
    ctx = MagicMock()

    res = await before_tool_callback(tool, args, ctx)
    assert res is None
    assert args["pr_number"] == 42


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
