"""Shared pytest fixtures for the hannibal-hub-agents suites.

Persistence gates must never leak from the developer shell into tests. direnv
exports ``ENABLE_REVIEW_*`` / ``ENABLE_FIRESTORE_REGISTRY`` alongside working
GCP credentials, which silently flips unit tests from the local-memory fallback
onto live Firestore (reads/writes against the production project). Every test
opts in explicitly via ``monkeypatch.setenv(...)`` when a live backend is
intentional (e.g. the ``e2e``-marked Firestore tests).
"""

from __future__ import annotations

import pytest

_PERSISTENCE_GATES = (
    "ENABLE_REVIEW_CHECKPOINT",
    "ENABLE_REVIEW_IDEMPOTENCY",
    "ENABLE_FIRESTORE_REGISTRY",
)


@pytest.fixture(autouse=True)
def _isolate_persistence_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force Firestore-backed subsystems onto their local fallback in tests."""
    for gate in _PERSISTENCE_GATES:
        monkeypatch.delenv(gate, raising=False)


@pytest.fixture(autouse=True)
def _disable_ci_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production holds reviews until CI is green; unit tests opt in explicitly.

    Without this, every processor test would depend on how a MagicMock GitHub client
    happens to answer the CI queries.
    """
    monkeypatch.setenv("REVIEW_WAIT_FOR_CI", "0")
