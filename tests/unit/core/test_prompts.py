"""Unit tests for core.prompts prompt constructors and message builders."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from webhook_agent.core.prompts import (
    AUDITOR_CONTEXT_INSTRUCTION,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    SYSTEM_INSTRUCTION,
    _truncate_input_for_tier,
    _truncate_text_to_token_limit,
    build_user_message,
    get_max_input_tokens,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_max_input_tokens_tier_resolution() -> None:
    """Test get_max_input_tokens respects paid vs free tier."""
    with patch("webhook_agent.core.prompts._resolve_tier", return_value="free"):
        assert get_max_input_tokens() == MAX_INPUT_TOKENS
        assert get_max_input_tokens() == 3500

    with patch("webhook_agent.core.prompts._resolve_tier", return_value="paid"):
        assert get_max_input_tokens() == 35000


def test_truncation_helpers_preserve_text() -> None:
    """Test _truncate_input_for_tier and _truncate_text_to_token_limit return text unchanged."""
    text = "Clinical audit payload with critical invariants."
    assert _truncate_input_for_tier(text) == text
    assert _truncate_text_to_token_limit(text) == text


def test_build_user_message_pull_request_opened() -> None:
    """Test build_user_message for PR opened event."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "action": "opened",
        "summary": "Fix race condition in worker loop",
        "raw_payload": {
            "pull_request": {
                "number": 42,
                "title": "Fix race condition",
                "body": "Resolves worker contention",
            },
            "pr_diff": "diff --git a/src/worker.py b/src/worker.py\n+ lock.acquire()",
            "changed_files": ["src/worker.py"],
        },
    }

    content = build_user_message(event_data)
    assert content.role == "user"
    assert len(content.parts) == 1
    assert content.parts[0].text is not None

    text = content.parts[0].text
    assert "Canonical Event: pull_request.opened" in text
    assert "Sender: developer" in text
    assert "Full PR Diff (Accumulated State):" in text
    assert "ACTION DIRECTIVE: FAST-PASS FORMAL AUDIT" in text


def test_build_user_message_conversational_comment() -> None:
    """Test build_user_message for issue comment event."""
    event_data: dict[str, Any] = {
        "canonical": "issue_comment.created",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "action": "created",
        "summary": "User comment on PR",
        "raw_payload": {
            "issue": {"number": 10},
            "comment": {"body": "Could you check if this function is idempotent?"},
        },
    }

    content = build_user_message(event_data)
    assert content.role == "user"
    assert len(content.parts) == 1
    assert content.parts[0].text is not None

    text = content.parts[0].text
    assert "Canonical Event: issue_comment.created" in text
    assert "Sender: developer" in text
    assert "Comment: Could you check if this function is idempotent?" in text


def test_instructions_exist_and_non_empty() -> None:
    """Test that all instruction prompt constants are non-empty strings."""
    assert len(SYSTEM_INSTRUCTION) > 100
    assert len(AUDITOR_CONTEXT_INSTRUCTION) > 100
    assert len(CONVERSATIONAL_INSTRUCTION) > 100


def test_webhook_agent_build_user_message_delegation_parity() -> None:
    """Verify WebhookAgent._build_user_message delegates identically to build_user_message."""
    from webhook_agent.core.agent_definition import WebhookAgent

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "raw_payload": {"pull_request": {"number": 123}},
    }
    agent = WebhookAgent(dry_run=True)
    delegated = agent._build_user_message(event_data)
    direct = build_user_message(event_data)
    assert delegated.role == direct.role
    assert delegated.parts[0].text == direct.parts[0].text
