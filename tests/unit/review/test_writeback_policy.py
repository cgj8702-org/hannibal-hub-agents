"""Unit tests for writeback policy and comment rate limiter."""

import time

from webhook_agent.review.writeback_policy import CommentRateLimiter


def test_purge_stale_records() -> None:
    """Verify that records older than max_age are evicted from memory."""
    limiter = CommentRateLimiter()
    old_time = time.time() - 7200.0
    limiter._history["test-repo#100"] = [old_time, old_time + 10.0]

    evicted = limiter.purge_stale_records(max_age=3600.0)

    assert evicted == 2
    assert "test-repo#100" not in limiter._history
