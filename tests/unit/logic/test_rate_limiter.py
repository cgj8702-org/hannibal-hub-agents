"""Unit tests for RPMWaiter rate limiter in hannibal-hub-agents."""

from pathlib import Path

import pytest

from logic.rate_limiter import RPMWaiter, _resolve_tier


@pytest.fixture
def mock_registry(tmp_path: Path) -> Path:
    registry_data = """{
    "models/gemini-3.5-flash-lite": {
        "free": { "rpm": 2, "tpm": 100, "rpd": 500.0 },
        "paid": { "rpm": 4, "tpm": 1000, "rpd": 150000.0 }
    },
    "models/gemini-2.0-flash": {
        "free": { "rpm": 0, "tpm": 0, "rpd": 0.0 },
        "paid": { "rpm": 2000, "tpm": 4000000, "rpd": 1000000.0 }
    }
}"""
    reg_file = tmp_path / "rate_limits.json"
    reg_file.write_text(registry_data, encoding="utf-8")
    return reg_file


def test_tier_resolution_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_TIER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("FREE_KEY", raising=False)
    monkeypatch.delenv("PAID_KEY", raising=False)

    # Default fallback when no env vars set -> free
    assert _resolve_tier() == "free"

    # Explicit WEBHOOK_TIER override
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    assert _resolve_tier() == "paid"
    monkeypatch.delenv("WEBHOOK_TIER")

    # Match active key against PAID_KEY
    monkeypatch.setenv("PAID_KEY", "pk_123")
    monkeypatch.setenv("FREE_KEY", "fk_456")
    monkeypatch.setenv("GEMINI_API_KEY", "pk_123")
    assert _resolve_tier() == "paid"

    # Match active key against FREE_KEY
    monkeypatch.setenv("GEMINI_API_KEY", "fk_456")
    assert _resolve_tier() == "free"

    # Active key non-matching, but non-dummy PAID_KEY present
    monkeypatch.setenv("GEMINI_API_KEY", "other_key")
    assert _resolve_tier() == "paid"

    # PAID_KEY set to dummy -> free
    monkeypatch.setenv("PAID_KEY", "dummy")
    assert _resolve_tier() == "free"


@pytest.mark.anyio
async def test_zero_quota_fast_fail(mock_registry: Path) -> None:
    waiter = RPMWaiter(registry_path=mock_registry)
    with pytest.raises(ValueError, match="0 quota"):
        await waiter.check_and_wait(model="gemini-2.0-flash", tier="free")


@pytest.mark.anyio
async def test_rpm_burst_pacing(mock_registry: Path) -> None:
    curr_time = 100.0

    def mock_clock() -> float:
        return curr_time

    waiter = RPMWaiter(registry_path=mock_registry, clock=mock_clock)
    # Model limit on free tier: rpm = 2
    await waiter.check_and_wait(model="gemini-3.5-flash-lite", tier="free")
    await waiter.check_and_wait(model="gemini-3.5-flash-lite", tier="free")

    assert len(waiter.histories["gemini-3.5-flash-lite"]) == 2


@pytest.mark.anyio
async def test_record_actual_tokens(mock_registry: Path) -> None:
    waiter = RPMWaiter(registry_path=mock_registry)
    await waiter.check_and_wait(
        model="gemini-3.5-flash-lite", estimated_tokens=50, tier="free"
    )
    assert waiter.token_histories["gemini-3.5-flash-lite"][0][1] == 50
    assert not waiter.token_histories["gemini-3.5-flash-lite"][0][2]

    await waiter.record_actual_tokens(model="gemini-3.5-flash-lite", actual_tokens=80)
    assert waiter.token_histories["gemini-3.5-flash-lite"][0][1] == 80
    assert waiter.token_histories["gemini-3.5-flash-lite"][0][2]
