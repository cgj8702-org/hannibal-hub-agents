"""Safety and loop avoidance plugins for feature_agent package.

Implements GuardrailsPlugin with NoProgressGuard and RepeatedFailureGuard.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin

logger = logging.getLogger("feature_agent.plugins")


class GuardrailsPlugin(BasePlugin):
    """ADK plugin enforcing loop-avoidance and repeated failure guards."""

    def __init__(self, max_repeated_failures: int = 3) -> None:
        super().__init__(name="guardrails_plugin")
        self.max_repeated_failures = max_repeated_failures
        self._last_edits: list[str] = []
        self._failure_counts: dict[str, int] = {}

    def after_model_callback(
        self, callback_context: CallbackContext, llm_response: Any
    ) -> Any:
        """NoProgressGuard: Detect if model generates identical content repeated turns."""
        text = getattr(llm_response, "text", "") or ""
        if text:
            if len(self._last_edits) >= 3 and all(
                e == text for e in self._last_edits[-3:]
            ):
                logger.warning(
                    "⚠️ NoProgressGuard triggered: model repeating identical edits 3x."
                )
                callback_context.state["halt_reason"] = "no_progress_loop"
            self._last_edits.append(text)
            if len(self._last_edits) > 10:
                self._last_edits.pop(0)
        return llm_response

    def after_tool_callback(
        self,
        tool: Any,
        args: dict[str, Any],
        tool_context: Any,
        tool_response: Any,
    ) -> Any:
        """RepeatedFailureGuard: Detect if pytest or ruff fails with identical output 3x in a row."""
        tool_name = str(getattr(tool, "name", "")) or str(tool)
        res_str = str(tool_response)

        if "🔴" in res_str or "FAILED" in res_str:
            err_sig = f"{tool_name}:{res_str[:150]}"
            count = self._failure_counts.get(err_sig, 0) + 1
            self._failure_counts[err_sig] = count

            if count >= self.max_repeated_failures:
                logger.warning(
                    "⚠️ RepeatedFailureGuard triggered: Tool '%s' failed %d times with same error.",
                    tool_name,
                    count,
                )
                state = getattr(tool_context, "state", None)
                if isinstance(state, dict):
                    state["halt_reason"] = f"repeated_failure:{tool_name}"
        return tool_response
