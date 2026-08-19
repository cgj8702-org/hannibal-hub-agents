"""Unit tests for centralized ADK Model Factory."""

import pytest
from google.adk.models import Gemini
from logic.model_factory import RateLimitedGemini, get_adk_model

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
