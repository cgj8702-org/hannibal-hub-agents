"""Backward-compatibility shim for models.depleted_registry."""

from webhook_agent.models.depleted_registry import (
    DepletedModelRegistry,
    FirestoreDepletedModelRegistry,
    firestore_depleted_registry,
)

__all__ = [
    "DepletedModelRegistry",
    "FirestoreDepletedModelRegistry",
    "firestore_depleted_registry",
]
