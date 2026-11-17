"""Sliding-window delivery throttling and Retry-After header parsing helpers."""

from __future__ import annotations

from typing import Any

_RECENT_DELIVERIES: dict[str, float] = {}


def allow_request(
    timestamps: list[float],
    now: float,
    window_seconds: float,
    max_requests: int,
) -> bool:
    """Return True if a request fits within the sliding window quota and record it."""
    active = [ts for ts in timestamps if now - ts < window_seconds]
    if len(active) > max_requests:
        timestamps[:] = active
        return False
    active.append(now)
    timestamps[:] = active
    return True


def record_delivery(delivery_id: str, now: float) -> bool:
    """Record a webhook delivery ID if unseen, returning False on duplicates."""
    if delivery_id in _RECENT_DELIVERIES:
        return False
    _RECENT_DELIVERIES[delivery_id] = now
    return True


def parse_retry_header(headers: dict[str, Any] | None) -> float | None:
    """Parse a numeric Retry-After HTTP header value into seconds."""
    if not headers:
        return None
    raw = headers["Retry-After"]
    text = str(raw).strip()
    if not text:
        return None
    try:
        delay = float(text)
    except ValueError:
        return None
    return delay if delay >= 0 else None
