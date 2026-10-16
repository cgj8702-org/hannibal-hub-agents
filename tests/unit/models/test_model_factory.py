"""Unit tests for centralized ADK Model Factory."""

import pytest
from google.adk.models import Gemini

from webhook_agent.models.model_factory import RateLimitedGemini, get_adk_model

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_get_adk_model_defaults() -> None:
    """Verify get_adk_model returns RateLimitedGemini instance with defaults."""
    model = get_adk_model(model_name="gemini-3.5-flash-lite", api_key="test_key")
    assert isinstance(model, RateLimitedGemini)
    assert isinstance(model, Gemini)
    assert model.model == "gemini-3.5-flash-lite"


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


def test_get_adk_model_paid_tier_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_adk_model on paid tier defaults to gemini-3.8-flash."""
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    model = get_adk_model(tier="paid", api_key="test_key")
    assert isinstance(model, RateLimitedGemini)
    assert model.model == "gemini-3.8-flash"


@pytest.mark.anyio
async def test_rate_limited_gemini_fails_fast_on_429(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 429 must propagate on the first attempt so the orchestrator can fail over.

    The model wrapper deliberately neither retries nor re-logs the Google error:
    the orchestrator in agent_definition is the single retry/failover and
    logging boundary, so the error flows up transparently.
    """
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import MagicMock

    model = RateLimitedGemini(model="gemini-3.5-flash-lite", client_kwargs={"api_key": "test_key"})

    attempts = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED: Please retry in 0.1s.")
        yield  # pragma: no cover

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    with pytest.raises(RuntimeError, match="429 RESOURCE_EXHAUSTED"):
        async for _ in model.generate_content_async(llm_request):
            pass

    assert attempts == 1
    # The wrapper must not print the Google error itself — no console spam.
    assert "hit quota/rate-limit" not in caplog.text
    assert "429 RESOURCE_EXHAUSTED" not in caplog.text


@pytest.mark.anyio
async def test_rate_limited_gemini_propagates_503_for_model_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 must reach the outer orchestrator instead of retrying the same model."""
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import MagicMock

    model = RateLimitedGemini(model="gemini-3.5-flash-lite", client_kwargs={"api_key": "test_key"})
    attempts = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("503 UNAVAILABLE: high demand")
        yield  # pragma: no cover

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    with pytest.raises(RuntimeError, match="503 UNAVAILABLE"):
        async for _ in model.generate_content_async(llm_request):
            pass

    assert attempts == 1


@pytest.mark.anyio
async def test_rate_limited_gemini_skips_count_tokens_when_rate_limit_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _rate_limit_checked is True, count_tokens and rpm_waiter must be bypassed."""
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import AsyncMock, MagicMock

    from webhook_agent.models.rate_limiter import rpm_waiter

    model = RateLimitedGemini(model="gemini-3.5-flash-lite", client_kwargs={"api_key": "test_key"})
    mock_api_client = MagicMock()
    mock_api_client.aio.models.count_tokens = AsyncMock()
    model.api_client = mock_api_client

    mock_check = AsyncMock()
    monkeypatch.setattr(rpm_waiter, "check_and_wait", mock_check)

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        yield "response_chunk"

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    chunks = []
    async for chunk in model.generate_content_async(llm_request):
        chunks.append(chunk)

    assert chunks == ["response_chunk"]
    mock_api_client.aio.models.count_tokens.assert_not_called()
    mock_check.assert_not_called()


@pytest.mark.anyio
async def test_rate_limited_gemini_calls_count_tokens_when_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _rate_limit_checked is False/absent, count_tokens and rpm_waiter must be called."""
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import AsyncMock, MagicMock

    from webhook_agent.models.rate_limiter import rpm_waiter

    model = RateLimitedGemini(model="gemini-3.5-flash-lite", client_kwargs={"api_key": "test_key"})
    mock_api_client = MagicMock()
    mock_ct_resp = MagicMock()
    mock_ct_resp.total_tokens = 42
    mock_api_client.aio.models.count_tokens = AsyncMock(return_value=mock_ct_resp)
    model.api_client = mock_api_client

    mock_check = AsyncMock()
    monkeypatch.setattr(rpm_waiter, "check_and_wait", mock_check)

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        yield "chunk"

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.model = "gemini-3.5-flash-lite"
    llm_request.contents = ["hello"]
    # _rate_limit_checked is not set
    del llm_request._rate_limit_checked

    chunks = []
    async for chunk in model.generate_content_async(llm_request):
        chunks.append(chunk)

    assert chunks == ["chunk"]
    mock_api_client.aio.models.count_tokens.assert_awaited_once_with(
        model="gemini-3.5-flash-lite", contents=["hello"]
    )
    mock_check.assert_awaited_once_with(
        model="gemini-3.5-flash-lite",
        estimated_tokens=42,
        tier="free",
    )
