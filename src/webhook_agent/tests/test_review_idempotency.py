"""Tests for PR/head review claim idempotency."""

from __future__ import annotations

import pytest

from webhook_agent.logic import review_idempotency
from webhook_agent.logic.review_idempotency import ReviewClaimRegistry

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_local_claim_suppresses_duplicate_and_records_submission() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    claim = registry.claim("owner/repo", 42, "head-a", github_review_exists=False)

    assert claim is not None
    assert registry.claim("owner/repo", 42, "head-a", github_review_exists=False) is None

    registry.mark_submitted(claim, "review-1", "https://example.test/review-1")
    assert registry.claim("owner/repo", 42, "head-a", github_review_exists=False) is None


def test_failed_claim_can_be_released_and_retried() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    claim = registry.claim("owner/repo", 42, "head-b", github_review_exists=False)
    assert claim is not None

    registry.release(claim)
    retry = registry.claim("owner/repo", 42, "head-b", github_review_exists=False)

    assert retry is not None
    assert retry.token != claim.token


def test_existing_github_review_reconciles_claim() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    assert registry.claim("owner/repo", 42, "head-c", github_review_exists=True) is None
    assert registry.claim("owner/repo", 42, "head-c", github_review_exists=False) is None


def test_firestore_uses_explicit_project_before_webhook_fallback(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_REVIEW_IDEMPOTENCY", "1")
    monkeypatch.setenv("FIRESTORE_PROJECT_ID", "explicit-firestore-project")
    monkeypatch.setenv("GCP_PROJECT_ID", "compute-host-project")
    monkeypatch.setenv("PUBSUB_PROJECT", "webhook-project")

    client = object()
    monkeypatch.setattr(review_idempotency, "_HAS_FIRESTORE", True)
    monkeypatch.setattr(review_idempotency.firestore, "Client", lambda project: client)
    registry = ReviewClaimRegistry()

    assert registry._get_db() is client


def test_firestore_uses_centralized_default_when_env_is_absent(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_REVIEW_IDEMPOTENCY", "1")
    monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
    monkeypatch.delenv("PUBSUB_PROJECT", raising=False)
    monkeypatch.setattr(review_idempotency, "_HAS_FIRESTORE", True)
    client = object()
    monkeypatch.setattr(review_idempotency.firestore, "Client", lambda project: client)
    registry = ReviewClaimRegistry()

    assert registry._get_db() is client
