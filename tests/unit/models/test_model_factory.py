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
    """Verify get_adk_model on paid tier defaults to gemini-3.5-flash-lite."""
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    model = get_adk_model(tier="paid", api_key="test_key")
    assert isinstance(model, RateLimitedGemini)
    assert model.model == "gemini-3.5-flash-lite"


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


@pytest.mark.anyio
async def test_rate_limited_gemini_retries_timeout_and_closes_stalled_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out attempt must close its generator before retrying and stamp custom_metadata."""
    import asyncio
    from collections.abc import AsyncGenerator
    from types import SimpleNamespace
    from typing import Any
    from unittest.mock import MagicMock

    from webhook_agent.models.model_factory import TIMEOUT_RETRIES_METADATA_KEY

    model = RateLimitedGemini(
        model="gemini-3.5-flash-lite",
        client_kwargs={"api_key": "test_key"},
        call_timeout_seconds=0.05,
        call_attempts=2,
    )

    events: list[str] = []
    attempts = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts
        attempts += 1
        current = attempts
        events.append(f"start:{current}")
        try:
            if current == 1:
                await asyncio.sleep(5.0)
            yield SimpleNamespace(text="ok", custom_metadata={"existing": True})
        finally:
            events.append(f"close:{current}")

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.model = "gemini-3.5-flash-lite"
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    chunks = [chunk async for chunk in model.generate_content_async(llm_request)]

    assert attempts == 2
    assert len(chunks) == 1
    assert chunks[0].text == "ok"
    assert chunks[0].custom_metadata == {
        "existing": True,
        TIMEOUT_RETRIES_METADATA_KEY: 1,
    }
    # Attempt 1's generator must be closed BEFORE attempt 2 starts.
    assert events == ["start:1", "close:1", "start:2", "close:2"]


@pytest.mark.anyio
async def test_rate_limited_gemini_propagates_timeout_after_exhausting_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every attempt times out, TimeoutError must propagate so execution can fail over."""
    import asyncio
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import MagicMock

    model = RateLimitedGemini(
        model="gemini-3.5-flash-lite",
        client_kwargs={"api_key": "test_key"},
        call_timeout_seconds=0.03,
        call_attempts=2,
    )

    closed: list[int] = []
    attempts = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts
        attempts += 1
        current = attempts
        try:
            await asyncio.sleep(5.0)
            yield "never"  # pragma: no cover
        finally:
            closed.append(current)

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.model = "gemini-3.5-flash-lite"
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    with pytest.raises(TimeoutError):
        async for _ in model.generate_content_async(llm_request):
            pass

    assert attempts == 2
    assert closed == [1, 2]


@pytest.mark.anyio
async def test_rate_limited_gemini_streaming_timeout_does_not_duplicate_yielded_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In streaming mode, a timeout after chunks were yielded propagates without retrying."""
    import asyncio
    from collections.abc import AsyncGenerator
    from typing import Any
    from unittest.mock import MagicMock

    model = RateLimitedGemini(
        model="gemini-3.5-flash-lite",
        client_kwargs={"api_key": "test_key"},
        call_timeout_seconds=0.05,
        call_attempts=3,
    )

    attempts = 0
    closed = 0

    async def mock_super_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal attempts, closed
        attempts += 1
        try:
            yield "partial_chunk"
            await asyncio.sleep(5.0)
        finally:
            closed += 1

    monkeypatch.setattr(Gemini, "generate_content_async", mock_super_gen)

    llm_request = MagicMock()
    llm_request.model = "gemini-3.5-flash-lite"
    llm_request.contents = ["hello"]
    llm_request._rate_limit_checked = True

    received: list[Any] = []
    with pytest.raises(TimeoutError):
        async for chunk in model.generate_content_async(llm_request, stream=True):
            received.append(chunk)

    assert received == ["partial_chunk"]
    assert attempts == 1
    assert closed == 1


def test_model_call_timeout_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify env parsing for MODEL_CALL_TIMEOUT_SECONDS and MODEL_CALL_ATTEMPTS."""
    from webhook_agent.models.model_factory import (
        _get_model_call_attempts,
        _get_model_call_timeout_seconds,
    )

    monkeypatch.delenv("MODEL_CALL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_CALL_ATTEMPTS", raising=False)
    assert _get_model_call_timeout_seconds() == 180.0
    assert _get_model_call_attempts() == 2

    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MODEL_CALL_ATTEMPTS", "0")
    assert _get_model_call_timeout_seconds() is None
    assert _get_model_call_attempts() == 1

    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setenv("MODEL_CALL_ATTEMPTS", "bad")
    assert _get_model_call_timeout_seconds() == 180.0
    assert _get_model_call_attempts() == 2

    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "nan")
    assert _get_model_call_timeout_seconds() == 180.0
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "inf")
    assert _get_model_call_timeout_seconds() == 180.0
