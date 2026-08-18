"""Unit tests for token optimization callbacks and plugins."""

import pytest
from unittest.mock import MagicMock
from src.token_optimized_agent.callbacks import (
    truncate_tool_response_callback,
    MessagePruningPlugin,
)

pytestmark = pytest.mark.unit


@pytest.mark.anyio
async def test_truncate_tool_response_callback_preserves_full_payload():
    mock_tool = MagicMock()
    mock_context = MagicMock()
    tool_response = {"data": list(range(100)), "text": "x" * 2000}

    result = await truncate_tool_response_callback(
        mock_tool, {}, mock_context, tool_response
    )

    assert result == tool_response
    assert len(result["data"]) == 100
    assert len(result["text"]) == 2000
    assert "_data_truncated" not in result


@pytest.mark.anyio
async def test_message_pruning_plugin():
    plugin = MessagePruningPlugin(max_history_events=5)
    mock_llm_request = MagicMock()
    mock_llm_request.contents = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    await plugin.before_model_callback(
        callback_context=MagicMock(), llm_request=mock_llm_request
    )

    assert mock_llm_request.contents == [6, 7, 8, 9, 10]


@pytest.mark.anyio
async def test_save_large_data_artifact():
    from unittest.mock import AsyncMock
    from src.token_optimized_agent.tools import save_large_data_artifact

    mock_context = MagicMock()
    mock_context.save_artifact = AsyncMock(return_value=1)

    result = await save_large_data_artifact(
        filename="dataset.csv", content="col1,col2\n1,2", tool_context=mock_context
    )

    assert result["status"] == "success"
    assert "version 1" in result["message"]
    mock_context.save_artifact.assert_called_once()
