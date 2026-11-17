"""Unit tests for sliding-window throttling and Retry-After header parsing."""

from __future__ import annotations

import pytest

from webhook_agent.state.retry_window import (
    _RECENT_DELIVERIES,
    allow_request,
    parse_retry_header,
    record_delivery,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_allow_request_records_within_window() -> None:
    timestamps: list[float] = []
    assert allow_request(timestamps, now=100.0, window_seconds=60.0, max_requests=5) is True
    assert timestamps == [100.0]


def test_record_delivery_deduplicates_id() -> None:
    _RECENT_DELIVERIES.clear()
    assert record_delivery("deliv-1", now=10.0) is True
    assert record_delivery("deliv-1", now=11.0) is False
    _RECENT_DELIVERIES.clear()


def test_parse_retry_header_valid_and_empty() -> None:
    assert parse_retry_header(None) is None
    assert parse_retry_header({}) is None
    assert parse_retry_header({"Retry-After": " 15.5 "}) == 15.5
    assert parse_retry_header({"Retry-After": "invalid"}) is None
