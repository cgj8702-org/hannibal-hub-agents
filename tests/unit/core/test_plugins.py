"""Unit tests for ADK Token Optimization Plugins."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from webhook_agent.core.plugins import (
    PRUNE_MARKER,
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.anyio
async def test_webhook_history_pruning_under_limit() -> None:
    plugin = WebhookHistoryPruningPlugin(max_events=5)
    ctx = MagicMock()
    req = MagicMock()
    req.contents = ["event0", "event1", "event2"]

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None
    assert req.contents == ["event0", "event1", "event2"]


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


@pytest.mark.anyio
async def test_webhook_history_pruning_no_contents() -> None:
    plugin = WebhookHistoryPruningPlugin(max_events=4)
    ctx = MagicMock()
    req = MagicMock(spec=[])

    res = await plugin.before_model_callback(callback_context=ctx, llm_request=req)
    assert res is None


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


@pytest.mark.anyio
async def test_prompt_sanitizer_plugin_string_and_content_parts() -> None:
    """Verify PromptSanitizerPlugin redacts secrets and returns None for callback chaining."""
    from webhook_agent.core.sanitizer_plugin import PromptSanitizerPlugin

    plugin = PromptSanitizerPlugin()
    ctx = MagicMock()

    # Test 1: string content with secret
    resp_str = MagicMock()
    resp_str.content = "Here is key AIzaSy012345678901234567890123456789123 ok"
    res1 = await plugin.after_model_callback(callback_context=ctx, llm_response=resp_str)
    assert res1 is None
    assert "[REDACTED_SECRET]" in resp_str.content
    assert "AIzaSy" not in resp_str.content

    # Test 2: Content object with parts
    resp_obj = MagicMock()
    part = MagicMock()
    part.text = "Here is token ghp_123456789012345678901234567890123456 in part"
    resp_obj.content = MagicMock()
    resp_obj.content.parts = [part]
    res2 = await plugin.after_model_callback(callback_context=ctx, llm_response=resp_obj)
    assert res2 is None
    assert "[REDACTED_SECRET]" in part.text
    assert "ghp_" not in part.text


def test_sanitize_markdown_text_widened_secret_formats() -> None:
    """Verify that widened secret formats (ghs, ghu, ghr, github_pat, AKIA, PEM) are redacted."""
    from webhook_agent.core.sanitizer_plugin import sanitize_markdown_text

    # 1. ghs_ installation token
    ghs_token = "ghs_" + "A" * 36
    res_ghs = sanitize_markdown_text(f"Installation token is {ghs_token}")
    assert ghs_token not in res_ghs
    assert "[REDACTED_SECRET]" in res_ghs

    # 2. ghu_ user-to-server token
    ghu_token = "ghu_" + "B" * 36
    res_ghu = sanitize_markdown_text(f"User token is {ghu_token}")
    assert ghu_token not in res_ghu
    assert "[REDACTED_SECRET]" in res_ghu

    # 3. ghr_ refresh token
    ghr_token = "ghr_" + "C" * 40
    res_ghr = sanitize_markdown_text(f"Refresh token is {ghr_token}")
    assert ghr_token not in res_ghr
    assert "[REDACTED_SECRET]" in res_ghr

    # 4. github_pat_ fine-grained token
    pat_token = "github_pat_" + "D" * 22
    res_pat = sanitize_markdown_text(f"PAT is {pat_token}")
    assert pat_token not in res_pat
    assert "[REDACTED_SECRET]" in res_pat

    # 5. AKIA AWS access key ID
    akia_token = "AKIA" + "1234567890ABCDEF"
    res_akia = sanitize_markdown_text(f"AWS key is {akia_token}")
    assert akia_token not in res_akia
    assert "[REDACTED_SECRET]" in res_akia

    # 6. PEM block with matching END marker
    pem_with_end = (
        "Config details:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEogIBAAKCAQEA0Y123456789\n"
        "-----END RSA PRIVATE KEY-----\n"
        "Remaining comment text."
    )
    res_pem_end = sanitize_markdown_text(pem_with_end)
    assert "-----BEGIN RSA PRIVATE KEY-----" not in res_pem_end
    assert "MIIEogIBAAKCAQEA0Y123456789" not in res_pem_end
    assert "[REDACTED_SECRET]" in res_pem_end
    assert "Remaining comment text." in res_pem_end

    # 7. PEM block without END marker (redacts through end of text)
    pem_without_end = (
        "Leaked key:\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQD"
    )
    res_pem_no_end = sanitize_markdown_text(pem_without_end)
    assert "-----BEGIN PRIVATE KEY-----" not in res_pem_no_end
    assert "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQD" not in res_pem_no_end
    assert "[REDACTED_SECRET]" in res_pem_no_end


def test_sanitize_markdown_text_negative_cases() -> None:
    """Verify that ordinary prose and short token look-alikes are preserved without false redacting."""
    from webhook_agent.core.sanitizer_plugin import sanitize_markdown_text

    # Ordinary prose
    prose = "This is a standard PR review comment without any secrets or credentials."
    assert sanitize_markdown_text(prose) == prose

    # Short ghs_ look-alike (fewer than 36 chars)
    short_ghs = "Check out ghs_123 for details."
    assert sanitize_markdown_text(short_ghs) == short_ghs

    # Short github_pat_ look-alike (fewer than 22 chars)
    short_pat = "Variable github_pat_foo is undefined."
    assert sanitize_markdown_text(short_pat) == short_pat

    # Short AKIA look-alike (fewer than 16 chars)
    short_akia = "Prefix AKIA123 is invalid."
    assert sanitize_markdown_text(short_akia) == short_akia

    # Public certificate PEM (not a private key)
    cert_pem = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIDXTCCAkWgAwIBAgIJAL...certificate\n"
        "-----END CERTIFICATE-----"
    )
    assert sanitize_markdown_text(cert_pem) == cert_pem
