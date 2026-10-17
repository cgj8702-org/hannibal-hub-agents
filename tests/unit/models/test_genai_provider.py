"""Unit tests for single-turn Gemini text generation provider seam."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.models.genai_provider import (
    GenerateContentProvider,
    TextGenerationResult,
    _extract_citations,
    _normalize_text,
    get_text_generation_provider,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_normalize_text() -> None:
    """Verify _normalize_text coerces various inputs correctly."""
    assert _normalize_text(None) == ""
    assert _normalize_text("hello world") == "hello world"
    assert _normalize_text(12345) == "12345"

    class MockObj:
        def __str__(self) -> str:
            return "<MagicMock name='"

    assert _normalize_text(MockObj()) == ""


def test_extract_citations_empty() -> None:
    """Verify citations extraction returns empty list on empty response."""
    empty_resp = MagicMock()
    empty_resp.candidates = None
    empty_resp.steps = None
    assert _extract_citations(empty_resp) == []


def test_extract_citations_from_grounding_chunks() -> None:
    """Verify citations extraction from candidate grounding metadata."""
    resp = MagicMock()
    candidate = MagicMock()
    chunk1 = MagicMock()
    chunk1.web.title = "Google Documentation"
    chunk1.web.uri = "https://cloud.google.com/docs"

    chunk2 = MagicMock()
    chunk2.web.title = ""
    chunk2.web.uri = "https://example.com/no-title"

    # Duplicate URI should be deduplicated
    chunk3 = MagicMock()
    chunk3.web.title = "Duplicate Link"
    chunk3.web.uri = "https://cloud.google.com/docs"

    # Empty URI should be skipped
    chunk4 = MagicMock()
    chunk4.web.title = "Empty URI"
    chunk4.web.uri = ""

    candidate.grounding_metadata.grounding_chunks = [chunk1, chunk2, chunk3, chunk4]
    resp.candidates = [candidate]
    resp.steps = None

    citations = _extract_citations(resp)
    assert citations == [
        "- [Google Documentation](https://cloud.google.com/docs)",
        "- [Source](https://example.com/no-title)",
    ]


def test_extract_citations_from_steps() -> None:
    """Verify citations extraction from step results (dict and object formats)."""
    resp = MagicMock()
    resp.candidates = []

    step1 = MagicMock()
    # Dict format with uri and title
    step1.result = [
        {"uri": "https://python.org", "title": "Python Official"},
        {"url": "https://pytest.org", "name": "Pytest Docs"},
        {"uri": None},  # No URI
    ]

    step2 = MagicMock()
    # Object format with url and title
    obj_result = MagicMock()
    obj_result.uri = None
    obj_result.url = "https://github.com"
    obj_result.title = "GitHub"
    obj_result.name = None
    step2.result = [obj_result]

    step3 = MagicMock()
    # Single dict instead of list
    step3.result = {"uri": "https://single.org", "title": "Single Item"}

    resp.steps = [step1, step2, step3]

    citations = _extract_citations(resp)
    assert citations == [
        "- [Python Official](https://python.org)",
        "- [Pytest Docs](https://pytest.org)",
        "- [GitHub](https://github.com)",
        "- [Single Item](https://single.org)",
    ]


def test_text_generation_result_dataclass() -> None:
    """Verify TextGenerationResult default fields."""
    res = TextGenerationResult(text="output text")
    assert res.text == "output text"
    assert res.total_tokens == 0
    assert res.citations == []


def test_generate_content_provider_success() -> None:
    """Verify GenerateContentProvider invokes client models generate_content."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Generated code review"
    mock_response.usage_metadata.total_token_count = 142
    mock_response.candidates = []
    mock_response.steps = []
    mock_client.models.generate_content.return_value = mock_response

    provider = GenerateContentProvider(mock_client)
    res = provider.generate("gemini-3.5-flash-lite", "Review this diff", config={"temp": 0.2})

    mock_client.models.generate_content.assert_called_once_with(
        model="gemini-3.5-flash-lite",
        contents="Review this diff",
        config={"temp": 0.2},
    )
    assert res.text == "Generated code review"
    assert res.total_tokens == 142
    assert res.citations == []


def test_generate_content_provider_token_fallback_and_invalid() -> None:
    """Verify token fallback to total_tokens and handling of invalid token types."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Response text"
    mock_response.usage_metadata.total_token_count = 0
    mock_response.usage_metadata.total_tokens = 99
    mock_response.candidates = []
    mock_response.steps = []
    mock_client.models.generate_content.return_value = mock_response

    provider = GenerateContentProvider(mock_client)
    res = provider.generate("gemini-3.5-flash-lite", "Prompt")
    assert res.total_tokens == 99

    # Non-integer token value
    mock_response.usage_metadata.total_tokens = "not-an-int"
    res2 = provider.generate("gemini-3.5-flash-lite", "Prompt")
    assert res2.total_tokens == 0

    # No usage metadata
    mock_response.usage_metadata = None
    res3 = provider.generate("gemini-3.5-flash-lite", "Prompt")
    assert res3.total_tokens == 0


def test_get_text_generation_provider_factory() -> None:
    """Verify get_text_generation_provider factory creates GenerateContentProvider."""
    mock_client = MagicMock()
    provider = get_text_generation_provider(mock_client)
    assert isinstance(provider, GenerateContentProvider)
