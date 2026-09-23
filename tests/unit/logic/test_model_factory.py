"""Unit tests for centralized ADK Model Factory."""

import pytest
from google.adk.models import Gemini

from webhook_agent.logic.model_factory import RateLimitedGemini, get_adk_model

pytestmark = [pytest.mark.unit]


@pytest.mark.unit
def test_get_adk_model_defaults() -> None:
    """Verify get_adk_model returns RateLimitedGemini instance with defaults."""
    model = get_adk_model(model_name="gemini-3.5-flash-lite", api_key="test_key")
    assert isinstance(model, RateLimitedGemini)
    assert isinstance(model, Gemini)
    assert model.model == "gemini-3.5-flash-lite"


@pytest.mark.unit
def test_get_adk_model_standard_gemini() -> None:
    """Verify rate_limited=False returns standard Gemini model."""
    model = get_adk_model(
        model_name="gemini-3.6-flash",
        api_key="test_key",
        rate_limited=False,
    )
    assert isinstance(model, Gemini)
    assert not isinstance(model, RateLimitedGemini)
    assert model.model == "gemini-3.6-flash"


@pytest.mark.unit
def test_get_adk_model_paid_tier_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_adk_model on paid tier defaults to gemini-3.8-flash."""
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    model = get_adk_model(tier="paid", api_key="test_key")
    assert isinstance(model, RateLimitedGemini)
    assert model.model == "gemini-3.8-flash"


@pytest.mark.anyio
@pytest.mark.unit
async def test_rate_limited_gemini_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify RateLimitedGemini catches 429 and retries in-flight without aborting."""
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import MagicMock

    model = RateLimitedGemini(model="gemini-3.5-flash-lite", client_kwargs={"api_key": "test_key"})

    attempts = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: Please retry in 0.1s.")
        yield MagicMock(text="success")

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    responses = []
    async for resp in model.generate_content_async(llm_request):
        responses.append(resp)

    assert len(responses) == 1
    assert responses[0].text == "success"
    assert attempts == 2
