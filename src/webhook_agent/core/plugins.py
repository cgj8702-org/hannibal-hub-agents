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

logger = logging.getLogger("webhook_agent.core.plugins")

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
            # Compact bulky Turn 0 context on multi-turn retries/followups to prevent TPM blowup
            if len(llm_request.contents) > 1:
                self._compact_initial_turn(llm_request.contents[0])

            if len(llm_request.contents) > self.max_events:
                old_count = len(llm_request.contents)
                # Determine candidate start index
                start_idx = len(llm_request.contents) - (self.max_events - 1)

                # Ensure we never orphan a tool response (function_response) without
                # its preceding function call turn, which causes Gemini API 400 Bad Request.
                while start_idx > 1:
                    first_kept = llm_request.contents[start_idx]
                    parts = getattr(first_kept, "parts", None) or []
                    is_tool_response = getattr(first_kept, "role", "") == "tool" or any(
                        getattr(p, "function_response", None) is not None for p in parts
                    )
                    if is_tool_response:
                        start_idx -= 1
                    else:
                        break

                # Preserve initial turn [0] (system instructions and initial context)
                # and retain the most recent events
                llm_request.contents = [
                    llm_request.contents[0],
                    *llm_request.contents[start_idx:],
                ]
                logger.debug(
                    "✂️ WebhookHistoryPruningPlugin: Pruned model contents from %d to %d events",
                    old_count,
                    len(llm_request.contents),
                )
        return None

    @staticmethod
    def _compact_initial_turn(turn_content: Any) -> None:
        """Strip bulky diffs and AST dumps from Turn 0 once subsequent turns begin."""
        parts = getattr(turn_content, "parts", None)
        if isinstance(turn_content, dict):
            parts = turn_content.get("parts")

        if not parts or not isinstance(parts, list):
            return

        for part in parts:
            text = getattr(part, "text", None)
            is_dict_part = False
            if text is None and isinstance(part, dict):
                text = part.get("text")
                is_dict_part = True

            if not text or len(text) < 4000:
                continue

            distilled = text
            for section_marker in (
                "### 📂 Unified Diff / Changed Files",
                "### 🔍 Surgical AST Enclosing Scopes",
                "### 📊 Deterministic Pre-Audit Dossier",
                "### 📋 Pre-Fetched Prior Review Findings to Verify",
            ):
                if section_marker in distilled:
                    head, rest = distilled.split(section_marker, 1)
                    next_header_idx = rest.find("\n### ")
                    tail = rest[next_header_idx:] if next_header_idx != -1 else ""
                    distilled = (
                        head
                        + f"{section_marker}\n*(Context distilled for follow-up turns. Refer to Turn 1 in conversation history.)*\n"
                        + tail
                    )

            if len(distilled) < len(text):
                logger.info(
                    "Compacted Turn 0 initial prompt for multi-turn execution (%d -> %d chars)",
                    len(text),
                    len(distilled),
                )
                if is_dict_part:
                    part["text"] = distilled
                else:
                    part.text = distilled


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
            has_fn_response = any(
                getattr(p, "function_response", None) is not None for p in content.parts
            )
            if is_user or has_fn_response:
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
