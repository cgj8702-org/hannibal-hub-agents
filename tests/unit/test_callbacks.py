"""Unit tests for ADK Callbacks Suite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.callbacks import (
    MAX_TOOL_CHARS,
    ROUTE_CORE_BACKEND,
    ROUTE_DEV_DOCS,
    ROUTE_MINOR_FIX,
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    normalize_pr_scope_route,
    on_tool_error_callback,
    router_after_agent_callback,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


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
    from unittest.mock import AsyncMock

    from webhook_agent.logic.rate_limiter import rpm_waiter

    ctx = MagicMock()
    ctx.state = {"active_tier": "free"}
    ctx.agent.model = "gemini-3.5-flash-lite"

    req = MagicMock()
    req.contents = "test content"
    req.model = "gemini-3.5-flash-lite"

    mock_check = AsyncMock()
    monkeypatch.setattr(rpm_waiter, "check_and_wait", mock_check)

    res = await before_model_callback(ctx, req)
    assert res is None
    assert "prompt_tokens" in ctx.state
    assert ctx.state.get("active_model") == "gemini-3.5-flash-lite"
    mock_check.assert_awaited_once_with(
        model="gemini-3.5-flash-lite",
        estimated_tokens=ctx.state["prompt_tokens"],
        tier="free",
    )


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_after_model_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from webhook_agent.logic.rate_limiter import rpm_waiter

    ctx = MagicMock()
    ctx.state = {"active_model": "gemini-3.5-flash-lite"}

    resp = MagicMock()
    resp.usage_metadata.total_token_count = 125

    mock_record = AsyncMock()
    monkeypatch.setattr(rpm_waiter, "record_actual_tokens", mock_record)

    res = await after_model_callback(ctx, resp)
    assert res is None
    assert ctx.state.get("total_tokens") == 125
    mock_record.assert_awaited_once_with(
        model="gemini-3.5-flash-lite",
        actual_tokens=125,
    )


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
async def test_after_tool_callback_string_under_limit() -> None:
    tool = MagicMock()
    tool.name = "read_file"
    args = {"path": "main.py"}
    ctx = MagicMock()
    ctx.state = {}

    short_response = "short content"
    res = await after_tool_callback(tool, args, ctx, short_response)
    assert res == short_response


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_after_tool_callback_string_truncation() -> None:
    tool = MagicMock()
    tool.name = "read_file"
    args = {"path": "large_file.py"}
    ctx = MagicMock()
    ctx.state = {}

    long_response = "A" * (MAX_TOOL_CHARS + 5000)
    res = await after_tool_callback(tool, args, ctx, long_response)
    assert isinstance(res, str)
    assert res.startswith("A" * MAX_TOOL_CHARS)
    assert "Truncated 5000 characters" in res
    assert "[... Truncated" in res


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_after_tool_callback_dict_truncation() -> None:
    tool = MagicMock()
    tool.name = "get_diff"
    args = {"pr_number": 42}
    ctx = MagicMock()
    ctx.state = {}

    dict_response = {
        "status": "ok",
        "diff": "B" * (MAX_TOOL_CHARS + 2000),
    }
    res = await after_tool_callback(tool, args, ctx, dict_response)
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert res["diff"].startswith("B" * MAX_TOOL_CHARS)
    assert "Truncated 2000 characters" in res["diff"]


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.parametrize(
    ("raw_scope", "expected_route"),
    [
        ("dev_docs", ROUTE_DEV_DOCS),
        ("docs_only", ROUTE_DEV_DOCS),
        ("Documentation", ROUTE_DEV_DOCS),
        ("minor_fix", ROUTE_MINOR_FIX),
        ("fix", ROUTE_MINOR_FIX),
        ("core_backend", ROUTE_CORE_BACKEND),
        ("trivial_chore", ROUTE_CORE_BACKEND),
        ("", ROUTE_CORE_BACKEND),
        (None, ROUTE_CORE_BACKEND),
        (["core_backend"], ROUTE_CORE_BACKEND),
    ],
)
def test_normalize_pr_scope_route(raw_scope: object, expected_route: str) -> None:
    assert normalize_pr_scope_route(raw_scope) == expected_route


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_router_after_agent_callback_emits_route_and_state_delta() -> None:
    ctx = MagicMock()
    ctx.state = {"pr_scope": "dev_docs"}

    res = await router_after_agent_callback(ctx)

    assert res is None
    assert ctx.actions.route == ROUTE_DEV_DOCS
    # The state write is what forces ADK to emit the event carrying the route.
    assert ctx.state["pr_scope_route"] == ROUTE_DEV_DOCS


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.anyio
async def test_router_after_agent_callback_falls_back_to_core_backend() -> None:
    ctx = MagicMock()
    ctx.state = {}

    res = await router_after_agent_callback(ctx)

    assert res is None
    assert ctx.actions.route == ROUTE_CORE_BACKEND
    assert ctx.state["pr_scope_route"] == ROUTE_CORE_BACKEND
