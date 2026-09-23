"""Unit tests for Google Search grounding tool and programmatic quota limits."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.tools.search_tool import (
    MAX_SEARCH_CALLS_PER_SESSION,
    google_search_grounding_tool,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


class TestSearchTool:
    def test_empty_query_returns_error(self):
        ctx = MagicMock()
        ctx.state = {}
        res = google_search_grounding_tool(ctx, "")
        assert "Error: Empty search query" in res

    def test_session_limit_exceeded(self):
        ctx = MagicMock()
        ctx.state = {"search_count": MAX_SEARCH_CALLS_PER_SESSION}
        res = google_search_grounding_tool(ctx, "Python pytest best practices")
        assert "Error: Google Search tool limit reached" in res

    def test_search_execution_success(self):
        ctx = MagicMock()
        ctx.state = {}

        mock_candidate = MagicMock()
        mock_web = MagicMock()
        mock_web.title = "Pytest Docs"
        mock_web.uri = "https://docs.pytest.org"
        mock_chunk = MagicMock()
        mock_chunk.web = mock_web
        mock_candidate.grounding_metadata.grounding_chunks = [mock_chunk]

        mock_response = MagicMock()
        mock_response.text = "Pytest is a testing framework for Python."
        mock_response.candidates = [mock_candidate]

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            mock_client_cls.return_value = mock_client

            with patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
                res = google_search_grounding_tool(ctx, "pytest tutorial")

                assert "Google Search Results for: 'pytest tutorial'" in res
                assert "Pytest is a testing framework for Python." in res
                assert "[Pytest Docs](https://docs.pytest.org)" in res
                assert ctx.state["search_count"] == 1
                assert mock_client_cls.call_args.kwargs["http_options"].timeout == 15000

    def test_search_execution_uses_interactions_when_enabled(self, monkeypatch):
        ctx = MagicMock()
        ctx.state = {}
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("GEMINI_API_USE_INTERACTIONS", "true")

        mock_interaction = MagicMock()
        mock_interaction.output_text = "Pytest is a testing framework for Python."
        mock_interaction.steps = [
            MagicMock(
                type="google_search_result",
                result=[MagicMock(title="Pytest Docs", uri="https://docs.pytest.org")],
            )
        ]

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = mock_interaction
            mock_client_cls.return_value = mock_client

            res = google_search_grounding_tool(ctx, "pytest tutorial")

            assert "Pytest is a testing framework for Python." in res
            assert "[Pytest Docs](https://docs.pytest.org)" in res
            assert ctx.state["search_count"] == 1
            mock_client.interactions.create.assert_called_once()
            mock_client.models.generate_content.assert_not_called()
