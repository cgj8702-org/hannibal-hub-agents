"""Unit tests for CloudLoggingAnalyticsPlugin."""

from unittest.mock import MagicMock, patch

import pytest
from src.logic.analytics import CloudLoggingAnalyticsPlugin

pytestmark = [pytest.mark.unit]


@pytest.mark.unit
def test_cloud_logging_analytics_plugin_callbacks() -> None:
    plugin = CloudLoggingAnalyticsPlugin()
    mock_agent = MagicMock()
    mock_agent.name = "test_agent"
    mock_context = MagicMock()
    mock_context.state = {}
    mock_context.session_id = "test_session_123"

    with patch("src.logic.analytics.logger.info") as mock_log:
        plugin.before_agent_callback(mock_agent, mock_context)
        assert f"__start_time_{mock_agent.name}" in mock_context.state

        plugin.after_agent_callback(mock_agent, mock_context)
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert "execution finished" in args[0]
        assert kwargs["extra"]["adk_metrics"]["agent_name"] == "test_agent"


@pytest.mark.unit
def test_cloud_logging_analytics_model_and_tool_callbacks() -> None:
    plugin = CloudLoggingAnalyticsPlugin()
    mock_context = MagicMock()

    mock_llm = MagicMock()
    mock_llm.model = "gemini-3.5-flash-lite"
    mock_llm.contents = "sample prompt content"

    with patch("src.logic.analytics.logger.info") as mock_log:
        plugin.before_model_callback(mock_context, mock_llm)
        mock_log.assert_called_once()
        assert "Model request to '%s'" in mock_log.call_args[0][0]
        assert mock_log.call_args[0][1] == "gemini-3.5-flash-lite"

    with patch("src.logic.analytics.logger.info") as mock_log:
        plugin.after_tool_callback(
            "search_codebase", {}, mock_context, {"result": "ok"}
        )
        mock_log.assert_called_once()
        assert "Tool '%s' executed" in mock_log.call_args[0][0]
        assert mock_log.call_args[0][1] == "search_codebase"
