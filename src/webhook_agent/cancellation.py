"""Transitional backwards-compatibility shim for cancellation.

Relocated to webhook_agent.core.cancellation in Phase 6a modularization.
"""

from __future__ import annotations

from webhook_agent.core.cancellation import (
    AbortAgentExecution,
    PRClosedRegistry,
    logger,
    pr_closed_registry,
)

__all__ = [
    "AbortAgentExecution",
    "PRClosedRegistry",
    "logger",
    "pr_closed_registry",
]
