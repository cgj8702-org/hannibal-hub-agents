"""Unit tests for dual-tier key resolution, fast-fail enforcement, and allowed models filtering."""

import pytest
from src.logic.rate_limiter import (
    get_allowed_models,
    resolve_webhook_api_key,
)

pytestmark = pytest.mark.unit


def test_resolve_webhook_api_key_fast_fail_missing_free_key(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.delenv("WEBHOOK_FREE_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(
        RuntimeError, match="CRITICAL: Missing required secret 'WEBHOOK_FREE_KEY'"
    ):
        resolve_webhook_api_key()


def test_resolve_webhook_api_key_fast_fail_missing_paid_key(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.delenv("WEBHOOK_PAID_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(
        RuntimeError, match="CRITICAL: Missing required secret 'WEBHOOK_PAID_KEY'"
    ):
        resolve_webhook_api_key()


def test_get_allowed_models_filters_zero_quota(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    models = get_allowed_models("free")
    assert "gemma-4-31b-it" in models
    assert "gemma-4-26b-a4b-it" in models
    assert "gemini-2.0-flash" not in models
