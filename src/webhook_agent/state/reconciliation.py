"""In-flight audit deduplication and reconciliation claim management."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

RECONCILED_CACHE_TTL_SECONDS = 3600  # 1 hour

_RECONCILED_PR_CACHE: dict[str, float] = {}
_RECONCILIATION_LOCK = threading.Lock()


def try_claim_reconciliation(cache_key: str, now_ts: float | None = None) -> bool:
    """Atomically check and claim reconciliation for a PR head commit.

    Guards against concurrent thread execution across background sweeps and
    worker routines. Returns True if the claim was successfully acquired.
    """
    ts = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
    with _RECONCILIATION_LOCK:
        expired = [
            k
            for k, stored_ts in list(_RECONCILED_PR_CACHE.items())
            if ts - stored_ts > RECONCILED_CACHE_TTL_SECONDS
        ]
        for k in expired:
            _RECONCILED_PR_CACHE.pop(k, None)

        if cache_key in _RECONCILED_PR_CACHE:
            return False

        _RECONCILED_PR_CACHE[cache_key] = ts
        return True


def release_reconciliation_claim(cache_key: str) -> None:
    """Release a previously claimed reconciliation key, e.g. on execution failure."""
    with _RECONCILIATION_LOCK:
        _RECONCILED_PR_CACHE.pop(cache_key, None)


def is_reconciliation_claimed(cache_key: str, now_ts: float | None = None) -> bool:
    """Check if a reconciliation key is currently claimed without acquiring it."""
    ts = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
    with _RECONCILIATION_LOCK:
        if cache_key in _RECONCILED_PR_CACHE:
            if ts - _RECONCILED_PR_CACHE[cache_key] <= RECONCILED_CACHE_TTL_SECONDS:
                return True
            _RECONCILED_PR_CACHE.pop(cache_key, None)
        return False


def build_reconciliation_cache_key(repo_name: str, pr_number: int | str, head_sha: str) -> str:
    """Construct canonical reconciliation cache key."""
    return f"{repo_name}#{pr_number}#{head_sha}"


def clear_reconciliation_cache() -> None:
    """Atomically reset the reconciliation cache (used primarily for test isolation)."""
    with _RECONCILIATION_LOCK:
        _RECONCILED_PR_CACHE.clear()


__all__ = [
    "RECONCILED_CACHE_TTL_SECONDS",
    "build_reconciliation_cache_key",
    "clear_reconciliation_cache",
    "is_reconciliation_claimed",
    "release_reconciliation_claim",
    "try_claim_reconciliation",
]
