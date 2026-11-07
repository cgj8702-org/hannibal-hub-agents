"""Unit tests for UniversalJsonFormatter and json logging helpers."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

from webhook_agent.core.logging_helpers import (
    UniversalJsonFormatter,
    json_event,
    json_log,
)


def test_universal_json_formatter_plain_message() -> None:
    formatter = UniversalJsonFormatter()
    record = logging.LogRecord(
        name="webhook_agent.worker",
        level=logging.INFO,
        pathname="src/webhook_agent/worker.py",
        lineno=100,
        msg="Worker started, processing subscription",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["logger"] == "webhook_agent.worker"
    assert parsed["severity"] == "INFO"
    assert parsed["message"] == "Worker started, processing subscription"
    assert "timestamp" in parsed
    assert parsed["timestamp"].endswith("Z")
    assert parsed["sourceLocation"]["file"] == "src/webhook_agent/worker.py"
    assert parsed["sourceLocation"]["line"] == 100


def test_universal_json_formatter_with_json_fields_extra() -> None:
    formatter = UniversalJsonFormatter()
    record = logging.LogRecord(
        name="webhook_agent.processor",
        level=logging.WARNING,
        pathname="src/webhook_agent/processor.py",
        lineno=50,
        msg="Suppressed noisy event: %s",
        args=("push",),
        exc_info=None,
    )
    record.json_fields = {
        "event": "event_suppressed",
        "action": "push",
        "reason": "noise_filter",
    }
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["event"] == "event_suppressed"
    assert parsed["action"] == "push"
    assert parsed["reason"] == "noise_filter"
    assert parsed["message"] == "Suppressed noisy event: push"
    # Ensure json_fields remains intact on record for caplog and audit tests
    assert hasattr(record, "json_fields")


def test_universal_json_formatter_with_dict_msg() -> None:
    formatter = UniversalJsonFormatter()
    record = logging.LogRecord(
        name="webhook_agent.core.execution",
        level=logging.INFO,
        pathname="src/webhook_agent/core/execution.py",
        lineno=200,
        msg={"event": "investigation_step", "pr_number": 42, "status": "completed"},
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["event"] == "investigation_step"
    assert parsed["pr_number"] == 42
    assert parsed["status"] == "completed"
    assert parsed["message"] == "investigation_step"


def test_universal_json_formatter_with_json_string_msg() -> None:
    formatter = UniversalJsonFormatter()
    payload = json_event(
        event="tool_completed",
        tool="get_pr_diff",
        duration_s=1.2,
        status="success",
    )
    record = logging.LogRecord(
        name="webhook_agent.core.callbacks",
        level=logging.INFO,
        pathname="src/webhook_agent/core/callbacks.py",
        lineno=300,
        msg=payload,
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["event"] == "tool_completed"
    assert parsed["tool"] == "get_pr_diff"
    assert parsed["duration_s"] == 1.2
    assert parsed["status"] == "success"


def test_universal_json_formatter_with_exception() -> None:
    formatter = UniversalJsonFormatter()
    try:
        raise ValueError("test error calculation")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="webhook_agent.worker",
        level=logging.ERROR,
        pathname="src/webhook_agent/worker.py",
        lineno=120,
        msg="Error during processing",
        args=(),
        exc_info=exc_info,
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["severity"] == "ERROR"
    assert parsed["message"] == "Error during processing"
    assert "exception" in parsed
    assert "ValueError: test error calculation" in parsed["exception"]


def test_json_event_and_json_log_helpers() -> None:
    raw1 = json_event("worker_started", subscription="webhooks-sub")
    p1 = json.loads(raw1)
    assert p1["event"] == "worker_started"
    assert p1["subscription"] == "webhooks-sub"

    raw2 = json_log(event="auth_failed", reason="jwt_invalid", retry_count=3)
    p2 = json.loads(raw2)
    assert p2["event"] == "auth_failed"
    assert p2["reason"] == "jwt_invalid"
    assert p2["retry_count"] == 3
