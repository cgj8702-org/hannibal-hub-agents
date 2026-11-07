"""Telemetry and structured logging infrastructure.

Provides universal JSON formatting and structured event helpers across the repository.
"""

from __future__ import annotations

import collections.abc
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("webhook_agent.telemetry.logging")


class UniversalJsonFormatter(logging.Formatter):
    """Formats LogRecords into single-line JSON strings matching Cloud Logging jsonPayload specs.

    Guarantees:
    - Never mutates or deletes record.json_fields, preserving caplog assertions and unit test inspections.
    - If formatting for CloudLoggingHandler, outputs clean strings when json_fields is already present
      to prevent double-wrapping jsonPayload.
    - Produces strict, single-line JSON with ISO-8601 UTC timestamp, logger name, severity, and metadata.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        validate: bool = True,
        *,
        include_source_location: bool = True,
        is_cloud_logging_handler: bool = False,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.include_source_location = include_source_location
        self.is_cloud_logging_handler = is_cloud_logging_handler

    def format(self, record: logging.LogRecord) -> str:
        # Check if record carries structured extra json_fields
        json_fields = getattr(record, "json_fields", None)
        has_json_fields = isinstance(json_fields, collections.abc.Mapping)

        # When running on CloudLoggingHandler and record already has json_fields,
        # CloudLoggingHandler._format_and_parse_message already merges json_fields + message into jsonPayload.
        # Returning record.getMessage() prevents nesting the JSON payload inside a "message" sub-dictionary.
        if self.is_cloud_logging_handler and has_json_fields:
            return record.getMessage()

        # Base JSON payload fields
        iso_timestamp = (
            datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z")
        )

        payload: dict[str, Any] = {
            "timestamp": iso_timestamp,
            "severity": record.levelname,
            "logger": record.name,
        }

        # Source code location metadata
        if self.include_source_location:
            payload["sourceLocation"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }

        # Check if record.msg is already a mapping/dict
        if isinstance(record.msg, collections.abc.Mapping):
            payload.update(record.msg)
            if "message" not in record.msg:
                payload["message"] = str(record.msg.get("event") or "")
        else:
            formatted_msg = record.getMessage()
            # If formatted_msg is JSON, parse and unpack its fields
            if formatted_msg.startswith("{") and formatted_msg.endswith("}"):
                try:
                    parsed_json = json.loads(formatted_msg)
                    if isinstance(parsed_json, collections.abc.Mapping):
                        payload.update(parsed_json)
                        if "message" not in parsed_json:
                            payload["message"] = str(parsed_json.get("event") or "")
                    else:
                        payload["message"] = formatted_msg
                except Exception:
                    payload["message"] = formatted_msg
            else:
                payload["message"] = formatted_msg

        # Extract structured extra fields without mutating or deleting the record's attribute
        if isinstance(json_fields, collections.abc.Mapping):
            payload.update(json_fields)

        # Attach formatted exception traceback if present
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def json_event(event: str, message: str | None = None, **kwargs: Any) -> str:
    """Format an event dictionary into a serialized JSON string for loggers.

    Args:
        event: Canonical event name (e.g. 'investigation_step', 'worker_started').
        message: Optional human-readable message string.
        **kwargs: Additional structured attributes to include in the JSON payload.

    Returns:
        A serialized JSON string.
    """
    payload: dict[str, Any] = {"event": event}
    if message is not None:
        payload["message"] = message
    payload.update(kwargs)
    return json.dumps(payload, default=str)


def json_log(**kwargs: Any) -> str:
    """Alias for json_event for flexible invocation and backward compatibility."""
    event = kwargs.pop("event", "unknown_event")
    return json_event(event=event, **kwargs)
