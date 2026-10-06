"""Backward-compatibility re-export shim for core.plugins."""

from webhook_agent.core.plugins import (
    PRUNE_MARKER,
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)

__all__ = [
    "PRUNE_MARKER",
    "ToolOutputPruningPlugin",
    "WebhookHistoryPruningPlugin",
]
