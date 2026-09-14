"""ADK Token Optimization Plugins for Webhook Agent.

Implements:
- WebhookHistoryPruningPlugin: Prunes conversation turns beyond working window.
- ToolOutputPruningPlugin: Deterministically zeroes out stale tool outputs in session history.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin

logger = logging.getLogger("webhook_agent.logic.plugins")

PRUNE_MARKER = "[output pruned to reclaim context — re-run the tool if needed]"


class WebhookHistoryPruningPlugin(BasePlugin):
    """Retains only the latest N events in active model context for multi-turn sessions."""

    def __init__(self, max_events: int = 12, name: str = "history_pruning") -> None:
        super().__init__(name=name)
        self.max_events = max_events

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: Any
    ) -> Any:
        if hasattr(llm_request, "contents") and isinstance(llm_request.contents, list):
            if len(llm_request.contents) > self.max_events:
                old_count = len(llm_request.contents)
                # Preserve initial turn [0] (system instructions and initial context)
                # and retain the most recent (self.max_events - 1) events
                llm_request.contents = [
                    llm_request.contents[0],
                    *llm_request.contents[-(self.max_events - 1) :],
                ]
                logger.info(
                    "✂️ WebhookHistoryPruningPlugin: Pruned model contents from %d to %d events",
                    old_count,
                    len(llm_request.contents),
                )
        return None


class ToolOutputPruningPlugin(BasePlugin):
    """Zeroes large, stale tool-result bodies in session events before model calls."""

    def __init__(
        self,
        protect_recent_turns: int = 3,
        min_part_tokens: int = 500,
        min_reclaim_tokens: int = 2000,
        name: str = "tool_output_pruning",
    ) -> None:
        super().__init__(name=name)
        self.protect_recent_turns = protect_recent_turns
        self.min_part_tokens = min_part_tokens
        self.min_reclaim_tokens = min_reclaim_tokens

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: Any
    ) -> Any:
        inv = getattr(callback_context, "_invocation_context", None)
        session = getattr(inv, "session", None)
        events = getattr(session, "events", None)
        if not events:
            return None

        # Reclaim stale tool outputs in session events
        candidates: list[tuple[Any, int, int]] = []
        turns_seen = 0

        for event in reversed(events):
            content = getattr(event, "content", None)
            if not content or not getattr(content, "parts", None):
                continue

            is_user = any(getattr(p, "text", None) for p in content.parts) and not any(
                getattr(p, "function_response", None) or getattr(p, "function_call", None)
                for p in content.parts
            )
            if is_user:
                turns_seen += 1

            if turns_seen <= self.protect_recent_turns:
                continue

            for idx, part in enumerate(content.parts):
                fr = getattr(part, "function_response", None)
                if fr is None:
                    continue
                resp = getattr(fr, "response", None)
                if isinstance(resp, dict) and resp.get("pruned") == PRUNE_MARKER:
                    continue
                est_tokens = len(str(resp)) // 4
                if est_tokens >= self.min_part_tokens:
                    candidates.append((event, idx, est_tokens))

        total_reclaim = sum(t for _, _, t in candidates)
        if total_reclaim >= self.min_reclaim_tokens:
            for event, idx, _ in candidates:
                part = event.content.parts[idx]
                part.function_response.response = {"pruned": PRUNE_MARKER}
            logger.info(
                "🧹 ToolOutputPruningPlugin: Zeroed %d stale tool output(s), reclaimed ~%d tokens",
                len(candidates),
                total_reclaim,
            )

        return None
