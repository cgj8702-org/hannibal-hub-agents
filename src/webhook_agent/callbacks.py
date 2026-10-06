"""Transitional backwards-compatibility shim for callbacks.

Relocated to webhook_agent.core.callbacks in Phase 6a modularization.
"""

from __future__ import annotations

from webhook_agent.core.callbacks import (
    _check_pr_closed_short_circuit,
    after_model_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    logger,
    on_tool_error_callback,
    rpm_waiter,
)

__all__ = [
    "_check_pr_closed_short_circuit",
    "after_model_callback",
    "before_agent_callback",
    "before_model_callback",
    "before_tool_callback",
    "logger",
    "on_tool_error_callback",
    "rpm_waiter",
]
