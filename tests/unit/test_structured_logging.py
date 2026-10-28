"""Unit tests for structured Google Cloud Logging json_fields support."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.processor import WebhookProcessor
from webhook_agent.webhook_types import ActionResult

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_agent_action_logging_populates_json_fields(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure WebhookProcessor logs Agent action with extra['json_fields'] containing structured attributes."""
    monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
    processor = WebhookProcessor()

    mock_result = ActionResult(
        tool="review",
        success=True,
        detail="tool executed: {'result': 'Submitted review (APPROVE)'}",
        action_result={"result": "Submitted review (APPROVE)"},
    )

    payload = {
        "event_name": "pull_request",
        "action": "opened",
        "canonical": "pull_request.opened",
        "delivery_id": "del-12345",
        "repository": {"full_name": "test-org/test-repo"},
        "raw_payload": {
            "pull_request": {"number": 424, "state": "open", "head": {"sha": "abcdef12345"}},
            "repository": {"full_name": "test-org/test-repo"},
            "sender": {"login": "test-user"},
        },
    }

    with (
        patch("webhook_agent.processor.load_cached_token", return_value=MagicMock(token="fake")),
        patch("webhook_agent.processor.Github"),
        patch("webhook_agent.processor.AgentCore") as mock_core_cls,
        patch("webhook_agent.processor.evaluate_ci", return_value=MagicMock(value="pass")),
        patch("webhook_agent.processor.last_bot_review_sha", return_value=(False, None)),
        caplog.at_level(logging.DEBUG),
    ):
        mock_agent = MagicMock()
        mock_agent.run.return_value = [mock_result]
        mock_core_cls.return_value = mock_agent

        processor.process_event(payload, ci_verified=True)

    action_records = [
        r for r in caplog.records if "Agent action [OK] (tool: review)" in r.getMessage()
    ]
    assert len(action_records) == 1
    record = action_records[0]

    # Verify json_fields extra attribute is attached and properly structured
    json_fields = getattr(record, "json_fields", None)
    assert json_fields is not None
    assert json_fields["status"] == "OK"
    assert json_fields["tool"] == "review"
    assert json_fields["repo"] == "test-org/test-repo"
    assert json_fields["item_id"] == "424"
    assert json_fields["action_result"] == {"result": "Submitted review (APPROVE)"}
    assert json_fields["delivery_id"] == "del-12345"
    assert "model" in json_fields


def test_webhook_processing_started_and_completed_json_fields(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure Webhook processing started and completed log records include json_fields."""
    monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
    processor = WebhookProcessor()

    payload = {
        "event_name": "pull_request",
        "action": "opened",
        "canonical": "pull_request.opened",
        "delivery_id": "del-start-complete",
        "repository": {"full_name": "test-org/test-repo"},
        "raw_payload": {
            "pull_request": {"number": 99, "state": "open", "head": {"sha": "1234567"}},
            "repository": {"full_name": "test-org/test-repo"},
            "sender": {"login": "test-user"},
        },
    }

    with (
        patch("webhook_agent.processor.load_cached_token", return_value=MagicMock(token="fake")),
        patch("webhook_agent.processor.Github"),
        patch("webhook_agent.processor.AgentCore") as mock_core_cls,
        patch("webhook_agent.processor.evaluate_ci", return_value=MagicMock(value="pass")),
        patch("webhook_agent.processor.last_bot_review_sha", return_value=(False, None)),
        caplog.at_level(logging.DEBUG),
    ):
        mock_agent = MagicMock()
        mock_agent.run.return_value = []
        mock_core_cls.return_value = mock_agent

        processor.process_event(payload, ci_verified=True)

    started_records = [r for r in caplog.records if "Webhook processing started" in r.getMessage()]
    assert len(started_records) == 1
    assert "model=" in started_records[0].getMessage()
    started_fields = getattr(started_records[0], "json_fields", None)
    assert started_fields is not None
    assert started_fields["repo"] == "test-org/test-repo"
    assert started_fields["item_id"] == "99"
    assert started_fields["delivery_id"] == "del-start-complete"
    assert "model" in started_fields

    completed_records = [
        r for r in caplog.records if "Webhook processing completed" in r.getMessage()
    ]
    assert len(completed_records) == 1
    assert "model=" in completed_records[0].getMessage()
    completed_fields = getattr(completed_records[0], "json_fields", None)
    assert completed_fields is not None
    assert completed_fields["repo"] == "test-org/test-repo"
    assert completed_fields["item_id"] == "99"
    assert completed_fields["action_count"] == 0
    assert "model" in completed_fields


def test_extract_log_context_populates_model() -> None:
    """Ensure _extract_log_context includes the active model in its returned dict."""
    from webhook_agent.processor import _extract_log_context

    payload = {
        "event_name": "pull_request",
        "canonical": "pull_request.opened",
        "delivery_id": "del-test-model",
        "repository": {"full_name": "owner/repo"},
        "raw_payload": {
            "pull_request": {"number": 123},
        },
    }
    ctx = _extract_log_context(payload)
    assert ctx["repo"] == "owner/repo"
    assert ctx["item_id"] == "123"
    assert ctx["event"] == "pull_request.opened"
    assert "model" in ctx
    assert isinstance(ctx["model"], str)
    assert len(ctx["model"]) > 0
