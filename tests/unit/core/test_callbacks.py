"""Unit tests for ADK Callbacks Suite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.core.callbacks import (
    _extract_cached_tokens,
    _extract_total_tokens,
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.anyio
async def test_before_agent_callback() -> None:
    ctx = MagicMock()
    ctx.state = {}
    await before_agent_callback(ctx)
    assert "active_tier" in ctx.state
    assert ctx.state["active_tier"] in ("free", "paid")


@pytest.mark.anyio
async def test_before_model_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from webhook_agent.models.rate_limiter import rpm_waiter

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
    assert getattr(req, "_rate_limit_checked", False) is True


@pytest.mark.anyio
async def test_before_model_callback_uses_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from webhook_agent.models.rate_limiter import rpm_waiter

    ctx = MagicMock()
    ctx.state = {"active_tier": "paid"}

    req = MagicMock()
    req.contents = "some pr code content"
    req.model = "gemini-3.8-flash"

    mock_check = AsyncMock()
    monkeypatch.setattr(rpm_waiter, "check_and_wait", mock_check)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.total_tokens = 321
    mock_client.models.count_tokens.return_value = mock_resp

    import webhook_agent.core.loop_helpers as loop_helpers

    monkeypatch.setattr(loop_helpers, "get_shared_genai_client", lambda: mock_client)

    res = await before_model_callback(ctx, req)
    assert res is None
    assert ctx.state.get("prompt_tokens") == 321
    assert getattr(req, "_rate_limit_checked", False) is True
    mock_client.models.count_tokens.assert_called_once_with(
        model="gemini-3.8-flash", contents="some pr code content"
    )
    mock_check.assert_awaited_once_with(
        model="gemini-3.8-flash",
        estimated_tokens=321,
        tier="paid",
    )


@pytest.mark.anyio
async def test_after_model_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from webhook_agent.models.rate_limiter import rpm_waiter

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
    tool.name = "read_file"
    args = {"file_path": "main.py"}
    ctx = MagicMock()
    ctx.state = {}

    err = Exception("429 RESOURCE_EXHAUSTED")
    res = await on_tool_error_callback(tool, args, ctx, err)
    assert res is not None
    assert res["success"] is False
    assert "temporary limit or error" in res["detail"]


@pytest.mark.anyio
async def test_before_tool_callback_allow_multiple_tools() -> None:
    from webhook_agent.core.policy import Mode

    tool = MagicMock()
    tool.name = "review"
    args1 = {"pr_number": 1, "body": "{}"}
    ctx = MagicMock()
    ctx.state = {"agent_mode": Mode.FORMAL_AUDIT}

    res1 = await before_tool_callback(tool, args1, ctx)
    assert res1 is None

    tool2 = MagicMock()
    tool2.name = "read_file"
    args2 = {"file_path": "main.py"}
    res2 = await before_tool_callback(tool2, args2, ctx)
    assert res2 is None


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


@pytest.mark.anyio
async def test_after_tool_callback_string_truncation() -> None:
    tool = MagicMock()
    tool.name = "read_file"
    args = {"path": "large_file.py"}
    ctx = MagicMock()
    ctx.state = {}

    long_response = "A" * 50000
    res = await after_tool_callback(tool, args, ctx, long_response)
    assert isinstance(res, str)
    assert res == long_response


@pytest.mark.anyio
async def test_after_tool_callback_dict_truncation() -> None:
    tool = MagicMock()
    tool.name = "get_diff"
    args = {"pr_number": 42}
    ctx = MagicMock()
    ctx.state = {}

    dict_response = {
        "status": "ok",
        "diff": "B" * 50000,
    }
    res = await after_tool_callback(tool, args, ctx, dict_response)
    assert isinstance(res, dict)
    assert res == dict_response


def test_resilient_token_extraction_variants() -> None:
    """Verify _extract_total_tokens and _extract_cached_tokens handle all SDK and dict formats."""
    resp1 = MagicMock()
    resp1.usage_metadata.total_token_count = 1000
    resp1.usage_metadata.cached_content_token_count = 800
    assert _extract_total_tokens(resp1) == 1000
    assert _extract_cached_tokens(resp1) == 800

    resp2 = MagicMock()
    resp2.usage_metadata.total_token_count = 2000
    del resp2.usage_metadata.cached_content_token_count
    resp2.usage_metadata.cached_tokens = None
    resp2.usage_metadata.cache_tokens_details.cached_tokens = 1500
    assert _extract_total_tokens(resp2) == 2000
    assert _extract_cached_tokens(resp2) == 1500

    resp3 = MagicMock()
    resp3.usage_metadata = {
        "total_token_count": 3000,
        "cached_content_token_count": 2500,
    }
    assert _extract_total_tokens(resp3) == 3000
    assert _extract_cached_tokens(resp3) == 2500

    resp4 = MagicMock()
    resp4.usage_metadata = {
        "total_token_count": 4000,
        "cache_tokens_details": {"cached_tokens": 3500},
    }
    assert _extract_total_tokens(resp4) == 4000
    assert _extract_cached_tokens(resp4) == 3500

    resp5 = MagicMock()
    resp5.usage_metadata = None
    assert _extract_total_tokens(resp5) == 0
    assert _extract_cached_tokens(resp5) == 0


@pytest.mark.anyio
async def test_after_model_callback_falls_back_to_cache_metadata() -> None:
    """Verify that after_model_callback confirms cache reuse via cache_metadata if tokens omitted."""
    ctx = MagicMock()
    ctx.state = {"active_model": "gemini-3.5-flash-lite"}

    resp = MagicMock()
    resp.usage_metadata = None
    resp.cache_metadata.cache_name = (
        "projects/123/locations/us-central1/cachedContents/test-cache-id"
    )
    resp.content = None

    await after_model_callback(ctx, resp)

    assert (
        ctx.state.get("active_cache_name")
        == "projects/123/locations/us-central1/cachedContents/test-cache-id"
    )


@pytest.mark.anyio
async def test_before_tool_callback_pydantic_validation_success() -> None:
    """Valid arguments are sanitized and coerced by Pydantic gate."""
    tool = MagicMock()
    tool.name = "get_issue"
    args = {"issue_number": "50", "include_diff": False}
    ctx = MagicMock()
    ctx.state = {}

    res = await before_tool_callback(tool, args, ctx)
    assert res is None
    # issue_number aliased to number, string coerced to int
    assert args["number"] == 50
    assert "get_issue" in ctx.state["tools_executed"]


@pytest.mark.anyio
async def test_before_tool_callback_pydantic_validation_failure() -> None:
    """Invalid arguments return an error dict to short-circuit execution safely."""
    tool = MagicMock()
    tool.name = "read_file"
    args = {}  # Missing required file_path
    ctx = MagicMock()
    ctx.state = {}

    res = await before_tool_callback(tool, args, ctx)
    assert res is not None
    assert "error" in res
    assert "Tool 'read_file' argument validation failed" in res["error"]


def test_format_tool_args_for_logging() -> None:
    """_format_tool_args_for_logging retains values, redacts sensitive keys, and truncates long strings."""
    from webhook_agent.core.callbacks import _format_tool_args_for_logging

    args = {
        "file_path": "src/webhook_agent/worker.py",
        "pattern": "def main",
        "github_token": "ghp_secret12345",
        "api_key": "AIzaSySecret",
        "huge_content": "X" * 300,
    }
    formatted = _format_tool_args_for_logging(args, max_val_len=50)
    assert formatted["file_path"] == "src/webhook_agent/worker.py"
    assert formatted["pattern"] == "def main"
    assert formatted["github_token"] == "[REDACTED]"
    assert formatted["api_key"] == "[REDACTED]"
    assert formatted["huge_content"].startswith("'")
    assert "[truncated" in formatted["huge_content"]


@pytest.mark.anyio
async def test_before_tool_callback_logs_real_arguments(caplog: pytest.LogCaptureFixture) -> None:
    """before_tool_callback logs the actual tool arguments rather than just keys."""
    import logging

    tool = MagicMock()
    tool.name = "read_file"
    args = {"file_path": "src/webhook_agent/worker.py", "ref": "main"}
    ctx = MagicMock()
    ctx.state = {}

    with caplog.at_level(logging.DEBUG):
        await before_tool_callback(tool, args, ctx)

    assert any(
        "read_file" in record.message and "src/webhook_agent/worker.py" in record.message
        for record in caplog.records
    )
