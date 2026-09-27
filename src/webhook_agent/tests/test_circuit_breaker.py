"""Unit tests for Circuit Breaker retry handler and exponential backoff."""

from __future__ import annotations

import asyncio
import pytest

from webhook_agent.logic.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


@pytest.mark.asyncio
async def test_circuit_breaker_success() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
    assert cb.state == CircuitState.CLOSED

    async def success_func(x: int) -> int:
        return x * 2

    res = await cb.call_async(success_func, 21)
    assert res == 42
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_transient_failures() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, base_delay=0.01, max_retries=1)
    assert cb.state == CircuitState.CLOSED

    call_count = 0

    async def failing_func() -> None:
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Connection refused 503")

    with pytest.raises(ConnectionError):
        await cb.call_async(failing_func)

    # First failure with max_retries=1 tried twice (attempt 0 and 1)
    assert cb.state == CircuitState.CLOSED  # only 1 failure recorded in window if threshold=2? Wait, call_async records failure on exception per attempt.

    # Let's test until OPEN state
    with pytest.raises(ConnectionError):
        await cb.call_async(failing_func)

    assert cb.state == CircuitState.OPEN

    # Subsequent call while OPEN raises CircuitOpenError
    with pytest.raises(CircuitOpenError):
        await cb.call_async(failing_func)


def test_compute_backoff() -> None:
    cb = CircuitBreaker(base_delay=1.0)
    delay_0 = cb.compute_backoff(0)
    delay_1 = cb.compute_backoff(1)
    assert delay_0 >= 0.1
    assert delay_1 > delay_0
