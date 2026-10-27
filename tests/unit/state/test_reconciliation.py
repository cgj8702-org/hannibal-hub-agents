"""Unit tests for in-flight audit deduplication and reconciliation claim management."""

from __future__ import annotations

import concurrent.futures

import pytest

from webhook_agent.state.reconciliation import (
    build_reconciliation_cache_key,
    clear_reconciliation_cache,
    is_reconciliation_claimed,
    release_reconciliation_claim,
    try_claim_reconciliation,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture(autouse=True)
def clean_reconciliation_cache():
    clear_reconciliation_cache()
    yield
    clear_reconciliation_cache()


def test_build_reconciliation_cache_key_format():
    assert build_reconciliation_cache_key("owner/repo", 42, "deadbeef") == "owner/repo#42#deadbeef"


def test_try_claim_reconciliation_basic():
    key = "owner/repo#100#sha1"
    assert try_claim_reconciliation(key) is True
    # Second attempt fails (already claimed)
    assert try_claim_reconciliation(key) is False


def test_release_reconciliation_claim_allows_reclaim():
    key = "owner/repo#101#sha2"
    assert try_claim_reconciliation(key) is True
    assert try_claim_reconciliation(key) is False

    release_reconciliation_claim(key)
    assert try_claim_reconciliation(key) is True


def test_is_reconciliation_claimed_checks_without_acquiring():
    key = "owner/repo#102#sha3"
    assert is_reconciliation_claimed(key) is False
    assert try_claim_reconciliation(key) is True
    assert is_reconciliation_claimed(key) is True


def test_concurrent_threads_reconciliation_claim_is_thread_safe():
    cache_key = "cgj8702-org/hannibal-hub#300#abcdef1"
    now_ts = 1000000.0
    results: list[bool] = []

    def worker():
        return try_claim_reconciliation(cache_key, now_ts)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly 1 thread successfully claimed the reconciliation slot
    assert results.count(True) == 1
    assert results.count(False) == 19


def test_reconciliation_claim_ttl_expiration():
    key = "owner/repo#103#sha4"
    t0 = 1000.0
    assert try_claim_reconciliation(key, now_ts=t0) is True

    # 3599 seconds later: still active
    assert is_reconciliation_claimed(key, now_ts=t0 + 3599) is True
    assert try_claim_reconciliation(key, now_ts=t0 + 3599) is False

    # 3601 seconds later: expired
    assert is_reconciliation_claimed(key, now_ts=t0 + 3601) is False
    assert try_claim_reconciliation(key, now_ts=t0 + 3601) is True
