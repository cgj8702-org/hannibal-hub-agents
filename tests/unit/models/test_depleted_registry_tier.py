"""Depletion state must be isolated per tier: free-key quota says nothing about the paid key."""

from __future__ import annotations

import pytest

from webhook_agent.models.depleted_registry import FirestoreDepletedModelRegistry

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

MODEL = "gemini-3.8-flash"


@pytest.fixture
def registry() -> FirestoreDepletedModelRegistry:
    # conftest strips ENABLE_FIRESTORE_REGISTRY, so this stays on local memory.
    return FirestoreDepletedModelRegistry()


def test_free_depletion_does_not_block_paid(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    registry.mark_depleted(MODEL)
    assert registry.is_depleted(MODEL) is True

    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    assert registry.is_depleted(MODEL) is False


def test_paid_depletion_does_not_block_free(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    registry.mark_depleted(MODEL)

    monkeypatch.setenv("WEBHOOK_TIER", "free")
    assert registry.is_depleted(MODEL) is False


def test_tier_flip_restores_original_state(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    registry.mark_depleted(MODEL)
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    assert registry.is_depleted(MODEL) is False
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    assert registry.is_depleted(MODEL) is True


def test_doc_id_is_prefixed_with_active_tier(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    registry.mark_depleted(MODEL)
    assert f"paid_{MODEL}" in registry._depleted
    assert MODEL not in registry._depleted


def test_explicit_key_alias_overrides_tier_prefix(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    registry.mark_depleted(MODEL, key_alias="paid")
    assert f"paid_{MODEL}" in registry._depleted
    assert registry.is_depleted(MODEL, key_alias="paid") is True
    assert registry.is_depleted(MODEL) is False


def test_filter_chain_respects_active_tier(
    registry: FirestoreDepletedModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = [MODEL, "gemini-3.7-flash"]
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    registry.mark_depleted(MODEL)
    assert registry.filter_chain(chain) == ["gemini-3.7-flash"]

    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    assert registry.filter_chain(chain) == chain
