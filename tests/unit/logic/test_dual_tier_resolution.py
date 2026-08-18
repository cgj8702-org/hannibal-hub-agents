"""Unit tests for dual-tier key resolution, fast-fail enforcement, and allowed models filtering."""

import pytest
from src.logic.rate_limiter import (
    resolve_webhook_api_key,
    get_allowed_models,
)

pytestmark = pytest.mark.unit


def test_resolve_webhook_api_key_free(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.setenv("WEBHOOK_FREE_KEY", "test-free-key-123")
    monkeypatch.delenv("WEBHOOK_PAID_KEY", raising=False)
    monkeypatch.delenv("FREE_KEY", raising=False)
    monkeypatch.delenv("PAID_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    key, key_source, tier = resolve_webhook_api_key()
    assert key == "test-free-key-123"
    assert key_source == "WEBHOOK_FREE_KEY"
    assert tier == "free"


def test_resolve_webhook_api_key_paid(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.setenv("WEBHOOK_PAID_KEY", "test-paid-key-456")
    monkeypatch.delenv("WEBHOOK_FREE_KEY", raising=False)

    key, key_source, tier = resolve_webhook_api_key()
    assert key == "test-paid-key-456"
    assert key_source == "WEBHOOK_PAID_KEY"
    assert tier == "paid"


def test_resolve_webhook_api_key_fast_fail_missing_free_key(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.delenv("WEBHOOK_FREE_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "should-be-ignored")

    with pytest.raises(
        RuntimeError, match="CRITICAL: Missing required secret 'WEBHOOK_FREE_KEY'"
    ):
        resolve_webhook_api_key()


def test_resolve_webhook_api_key_fast_fail_missing_paid_key(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.delenv("WEBHOOK_PAID_KEY", raising=False)

    with pytest.raises(
        RuntimeError, match="CRITICAL: Missing required secret 'WEBHOOK_PAID_KEY'"
    ):
        resolve_webhook_api_key()


def test_get_allowed_models_filters_zero_quota(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    models = get_allowed_models("free")
    assert "gemma-4-31b-it" in models
    assert "gemma-4-26b-a4b-it" in models
    # gemini-2.0-flash has 0 quota on free tier in rate_limits.json
    assert "gemini-2.0-flash" not in models
