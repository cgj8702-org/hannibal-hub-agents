"""Callbacks and plugins for pruning payload sizes and model input events."""

from typing import Any
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool, ToolContext


async def truncate_tool_response_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: Any,
) -> Any:
    """Preserve full tool response payload without truncation."""
    return tool_response


class MessagePruningPlugin(BasePlugin):
    """Plugin to cap the maximum number of history events sent in LLM requests."""

    def __init__(self, max_history_events: int = 20) -> None:
        super().__init__(name="message_pruning_plugin")
        self.max_history_events = max_history_events

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> Any:
        if (
            hasattr(llm_request, "contents")
            and len(llm_request.contents) > self.max_history_events
        ):
            llm_request.contents = llm_request.contents[-self.max_history_events :]
        return None
