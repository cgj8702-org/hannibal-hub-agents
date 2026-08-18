"""Production Google Cloud Logging Analytics Plugin for ADK Agents.

Tracks agent invocation latency, step durations, tool execution metrics, and model call frequency,
emitting structured JSON logs directly to Cloud Logging (projects/cgj8702-webhook-agent/logs/python).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin

logger = logging.getLogger("logic.analytics")


class CloudLoggingAnalyticsPlugin(BasePlugin):
    """Production-grade Cloud Logging Analytics Plugin for ADK Apps."""

    def __init__(self, name: str = "cloud_logging_analytics"):
        super().__init__(name=name)

    def before_agent_callback(
        self, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        """Record agent execution start time in callback context state."""
        try:
            callback_context.state[f"__start_time_{agent.name}"] = time.perf_counter()
        except Exception as exc:
            logger.debug("Analytics start time record skipped: %s", exc)

    def after_agent_callback(
        self, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        """Emit structured JSON telemetry log for agent execution completion."""
        try:
            start_time = callback_context.state.get(f"__start_time_{agent.name}")
            duration_ms = (
                round((time.perf_counter() - start_time) * 1000, 2)
                if start_time
                else None
            )

            metrics = {
                "event_type": "adk_agent_execution",
                "agent_name": agent.name,
                "duration_ms": duration_ms,
                "session_id": getattr(callback_context, "session_id", "unknown"),
            }

            logger.info(
                "📊 ADK Analytics: Agent '%s' execution finished in %s ms",
                agent.name,
                duration_ms if duration_ms is not None else "N/A",
                extra={"adk_metrics": metrics},
            )
        except Exception as exc:
            logger.debug("Analytics end record skipped: %s", exc)

    def before_model_callback(
        self, callback_context: CallbackContext, llm_request: Any
    ) -> None:
        """Track model request invocation and token estimation."""
        try:
            model_name = getattr(llm_request, "model", "unknown")
            contents_str = str(getattr(llm_request, "contents", ""))
            est_tokens = max(1, len(contents_str) // 4)

            metrics = {
                "event_type": "adk_model_request",
                "model_name": model_name,
                "estimated_tokens": est_tokens,
            }
            logger.info(
                "📊 ADK Analytics: Model request to '%s' (~%d tokens)",
                model_name,
                est_tokens,
                extra={"adk_metrics": metrics},
            )
        except Exception as exc:
            logger.debug("Analytics model record skipped: %s", exc)

    def after_tool_callback(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        callback_context: CallbackContext,
        tool_response: Any,
    ) -> None:
        """Track tool invocation status and potential errors."""
        try:
            is_error = isinstance(tool_response, dict) and "error" in tool_response
            metrics = {
                "event_type": "adk_tool_execution",
                "tool_name": tool_name,
                "is_error": is_error,
            }
            logger.info(
                "📊 ADK Analytics: Tool '%s' executed (error=%s)",
                tool_name,
                is_error,
                extra={"adk_metrics": metrics},
            )
        except Exception as exc:
            logger.debug("Analytics tool record skipped: %s", exc)
