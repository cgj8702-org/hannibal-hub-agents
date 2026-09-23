"""Tests for PR/head review claim idempotency."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from webhook_agent.logic import review_idempotency
from webhook_agent.logic.review_idempotency import ReviewClaimRegistry

pytestmark = [pytest.mark.webhook_agent]


@pytest.mark.unit
def test_local_claim_suppresses_duplicate_and_records_submission() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    claim = registry.claim("owner/repo", 42, "head-a", github_review_exists=False)

    assert claim is not None
    assert registry.claim("owner/repo", 42, "head-a", github_review_exists=False) is None

    registry.mark_submitted(claim, "review-1", "https://example.test/review-1")
    assert registry.claim("owner/repo", 42, "head-a", github_review_exists=False) is None


@pytest.mark.unit
def test_failed_claim_can_be_released_and_retried() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    claim = registry.claim("owner/repo", 42, "head-b", github_review_exists=False)
    assert claim is not None

    registry.release(claim)
    retry = registry.claim("owner/repo", 42, "head-b", github_review_exists=False)

    assert retry is not None
    assert retry.token != claim.token


@pytest.mark.unit
def test_existing_github_review_reconciles_claim() -> None:
    registry = ReviewClaimRegistry()
    registry._initialized = True

    assert registry.claim("owner/repo", 42, "head-c", github_review_exists=True) is None
    assert registry.claim("owner/repo", 42, "head-c", github_review_exists=False) is None


@pytest.mark.unit
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


@pytest.mark.unit
def test_firestore_uses_centralized_default_when_env_is_absent(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_REVIEW_IDEMPOTENCY", "1")
    monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
    monkeypatch.delenv("PUBSUB_PROJECT", raising=False)
    monkeypatch.setattr(review_idempotency, "_HAS_FIRESTORE", True)
    client = object()
    monkeypatch.setattr(review_idempotency.firestore, "Client", lambda project: client)
    registry = ReviewClaimRegistry()

    assert registry._get_db() is client


@pytest.mark.integration
@pytest.mark.firestore
def test_live_firestore_claim_is_atomic_across_registry_instances(monkeypatch) -> None:
    """Verify the production Firestore transaction path across two workers."""
    if os.getenv("RUN_FIRESTORE_INTEGRATION", "0").lower() not in ("1", "true"):
        pytest.skip("Set RUN_FIRESTORE_INTEGRATION=1 to run the live Firestore test")
    if not review_idempotency._HAS_FIRESTORE:
        pytest.skip("google-cloud-firestore is not installed")
    if not os.getenv("FIRESTORE_PROJECT_ID"):
        pytest.skip("FIRESTORE_PROJECT_ID is not configured")

    monkeypatch.setenv("ENABLE_REVIEW_IDEMPOTENCY", "1")
    collection_name = f"pr_review_claims_test_{uuid4().hex}"
    repo_name = "integration-test/review-claims"
    pr_number = 1
    head_sha = uuid4().hex
    registries = [ReviewClaimRegistry(collection_name=collection_name) for _ in range(2)]
    claims = []

    try:
        databases = [registry._get_db() for registry in registries]
        assert all(databases)

        def claim(registry: ReviewClaimRegistry):
            return registry.claim(repo_name, pr_number, head_sha, github_review_exists=False)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, registries))

        assert sum(item is not None for item in claims) == 1
        winner = next(item for item in claims if item is not None)
        registries[0].mark_submitted(winner, "integration-review", "https://example.test/review")
    finally:
        database = registries[0]._get_db()
        if database is not None:
            key = registries[0]._key(repo_name, pr_number, head_sha)
            database.collection(collection_name).document(key).delete()
