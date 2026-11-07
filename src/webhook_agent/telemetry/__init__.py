"""Telemetry package for webhook_agent."""

from __future__ import annotations

from webhook_agent.telemetry.logging import (
    UniversalJsonFormatter,
    json_event,
    json_log,
)

__all__ = [
    "UniversalJsonFormatter",
    "json_event",
    "json_log",
]
