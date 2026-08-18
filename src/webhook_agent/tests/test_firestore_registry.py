"""Unit tests for FirestoreDepletedModelRegistry in logic.firestore_registry."""

from __future__ import annotations

import time
import pytest
from unittest.mock import MagicMock

from logic.firestore_registry import FirestoreDepletedModelRegistry

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.firestore]


def test_firestore_registry_local_memory_fallback():
    registry = FirestoreDepletedModelRegistry(default_cooldown=3600.0)
    error = Exception("429 ResourceExhausted: Requests perDay limit reached")

    registry.mark_depleted("test-model", error=error, key_alias="FREE_KEY_1")

    assert registry.is_depleted("test-model", key_alias="FREE_KEY_1") is True
    assert registry.is_depleted("other-model", key_alias="FREE_KEY_1") is False


def test_firestore_registry_local_memory_expiration():
    registry = FirestoreDepletedModelRegistry(default_cooldown=3600.0)
    doc_id = "FREE_KEY_1_expired-model"
    registry._local_depleted[doc_id] = (time.time() - 100.0, 50.0)

    assert registry.is_depleted("expired-model", key_alias="FREE_KEY_1") is False
    assert doc_id not in registry._local_depleted


def test_firestore_registry_firestore_write_invocation(monkeypatch):
    monkeypatch.setenv("ENABLE_FIRESTORE_REGISTRY", "1")
    registry = FirestoreDepletedModelRegistry(default_cooldown=3600.0)

    mock_db = MagicMock()
    registry._db = mock_db
    registry._initialized = True

    registry.mark_depleted("test-fs-model", key_alias="PAID_KEY_1")

    mock_db.collection.assert_called_with("depleted_models")
    mock_db.collection().document.assert_called_with("PAID_KEY_1_test-fs-model")
