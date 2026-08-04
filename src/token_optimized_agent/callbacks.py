"""Callbacks and plugins for pruning payload sizes and model input events."""

import json
from typing import Any
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool, ToolContext


async def truncate_tool_response_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: Any,
) -> Any:
    """Truncate tool response payloads to prevent context overflow.

    Default limits:
    - Lists/arrays capped at 5 elements
    - String fields capped at 1,000 characters
    - Total serialized JSON payload capped at 4,000 characters
    """
    max_items = 5
    max_char_len = 1000
    max_total_payload = 4000

    if isinstance(tool_response, dict):
        for key, val in list(tool_response.items()):
            if isinstance(val, list) and len(val) > max_items:
                tool_response[key] = val[:max_items]
                tool_response[f"_{key}_truncated"] = True
            elif isinstance(val, str) and len(val) > max_char_len:
                tool_response[key] = val[:max_char_len] + "... [TRUNCATED]"

        serialized = json.dumps(tool_response)
        if len(serialized) > max_total_payload:
            tool_response = {
                "summary": serialized[:max_total_payload] + "... [PAYLOAD TRUNCATED]",
                "_payload_truncated": True,
            }
        return tool_response

    if isinstance(tool_response, list) and len(tool_response) > max_items:
        return tool_response[:max_items]

    if isinstance(tool_response, str) and len(tool_response) > max_char_len:
        return tool_response[:max_char_len] + "... [TRUNCATED]"

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
