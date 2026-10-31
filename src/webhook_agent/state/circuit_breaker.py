"""Circuit Breaker Retry Handler for external GitHub API calls and transient error resilience."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""

    def __init__(self, message: str = "Circuit breaker is OPEN. Requests blocked.") -> None:
        super().__init__(message)


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        window_seconds: float = 60.0,
        base_delay: float = 1.0,
        max_retries: int = 3,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.window_seconds = window_seconds
        self.base_delay = base_delay
        self.max_retries = max_retries

        self.state = CircuitState.CLOSED
        self.failure_timestamps: list[float] = []
        self.last_state_change_time = time.time()
        self.consecutive_failures = 0
        self.lock = asyncio.Lock()

    def _clean_old_failures(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self.failure_timestamps = [t for t in self.failure_timestamps if t >= cutoff]

    def _check_state_transition(self, now: float) -> None:
        if self.state == CircuitState.OPEN:
            if now - self.last_state_change_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.last_state_change_time = now

    def record_success(self) -> None:
        now = time.time()
        self._check_state_transition(now)
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.failure_timestamps.clear()
            self.last_state_change_time = now
        elif self.state == CircuitState.CLOSED:
            self.consecutive_failures = 0
            self.failure_timestamps.clear()

    def record_failure(self) -> None:
        now = time.time()
        self._check_state_transition(now)
        self._clean_old_failures(now)
        self.failure_timestamps.append(now)
        self.consecutive_failures += 1

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.last_state_change_time = now
        elif self.state == CircuitState.CLOSED:
            if (
                len(self.failure_timestamps) >= self.failure_threshold
                or self.consecutive_failures >= self.failure_threshold
            ):
                self.state = CircuitState.OPEN
                self.last_state_change_time = now

    def compute_backoff(self, attempt: int) -> float:
        """Compute exponential backoff with randomized multiplicative jitter: base_delay * 2^attempt * uniform(0.8, 1.2)."""
        raw_delay = self.base_delay * (2**attempt)
        jitter = random.uniform(0.8, 1.2)
        return max(0.1, round(raw_delay * jitter, 3))

    async def call_async(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute an asynchronous function call protected by circuit breaker and retry with jitter."""
        async with self.lock:
            now = time.time()
            self._check_state_transition(now)

            if self.state == CircuitState.OPEN:
                time_remaining = max(
                    0.0,
                    self.recovery_timeout - (now - self.last_state_change_time),
                )
                raise CircuitOpenError(
                    f"Circuit breaker is OPEN. State transition pending in {time_remaining:.1f}s."
                )

        last_exception: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                res = await func(*args, **kwargs)
                async with self.lock:
                    self.record_success()
                return res
            except Exception as e:
                last_exception = e
                err_str = str(e).lower()
                is_transient = any(
                    code in err_str
                    for code in (
                        "429",
                        "503",
                        "rate limit",
                        "timeout",
                        "connection",
                        "temporary",
                    )
                )

                async with self.lock:
                    self.record_failure()
                    is_open = self.state == CircuitState.OPEN

                if not is_transient and attempt == 0:
                    raise

                if attempt >= self.max_retries or is_open:
                    break

                delay = self.compute_backoff(attempt)
                await asyncio.sleep(delay)

        if last_exception:
            raise last_exception
        raise CircuitOpenError("Circuit breaker tripped during execution retries.")
