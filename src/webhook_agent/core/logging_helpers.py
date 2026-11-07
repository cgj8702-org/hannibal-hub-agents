"""Universal JSON logging infrastructure and structured event helpers.

Re-exported from webhook_agent.telemetry for backwards compatibility.
"""

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
