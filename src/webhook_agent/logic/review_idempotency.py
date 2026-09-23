"""Durable PR review claims used to suppress cross-worker duplicates."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from .constants import DEFAULT_FIRESTORE_PROJECT

logger = logging.getLogger("review_idempotency")

try:
    from google.cloud import firestore  # type: ignore[attr-defined]

    _HAS_FIRESTORE = True
except ImportError:
    firestore = None
    _HAS_FIRESTORE = False


@dataclass(frozen=True)
class ReviewClaim:
    """A claim token that must be completed or released after submission."""

    key: str
    token: str


class ReviewClaimRegistry:
    """Atomically claim one repository/PR/head review across workers.

    Firestore is enabled explicitly with ``ENABLE_REVIEW_IDEMPOTENCY``. Local
    memory is retained only for development and unit tests when Firestore is
    unavailable; production deployments should enable the durable backend.
    """

    def __init__(self, collection_name: str = "pr_review_claims", lease_seconds: int = 300) -> None:
        self.collection_name = collection_name
        self.lease_seconds = lease_seconds
        self._db: Any = None
        self._initialized = False
        self._local_claims: dict[str, tuple[str, float, str]] = {}
        self._local_lock = threading.Lock()

    @staticmethod
    def _key(repo_name: str, pr_number: int, head_sha: str) -> str:
        raw_key = f"{repo_name}:{pr_number}:{head_sha}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _get_db(self) -> Any | None:
        if not self._initialized:
            self._initialized = True
            enabled = os.getenv("ENABLE_REVIEW_IDEMPOTENCY", "0").lower() in (
                "1",
                "true",
            )
            if _HAS_FIRESTORE and enabled:
                try:
                    project_id = (
                        os.getenv("FIRESTORE_PROJECT_ID")
                        or os.getenv("PUBSUB_PROJECT")
                        or DEFAULT_FIRESTORE_PROJECT
                    )
                    self._db = firestore.Client(project=project_id)
                    logger.info(
                        "Initialized durable review claim registry for project [%s]",
                        project_id,
                    )
                except Exception as exc:
                    logger.warning("Could not initialize review claim registry: %s", exc)
        return self._db

    def claim(
        self,
        repo_name: str,
        pr_number: int,
        head_sha: str,
        github_review_exists: bool,
    ) -> ReviewClaim | None:
        """Claim a review unless this head is already submitted or in progress."""
        key = self._key(repo_name, pr_number, head_sha)
        token = uuid.uuid4().hex
        now = datetime.datetime.now(datetime.UTC)
        lease_until = now + datetime.timedelta(seconds=self.lease_seconds)
        db = self._get_db()

        if db is None:
            with self._local_lock:
                existing = self._local_claims.get(key)
                if github_review_exists:
                    self._local_claims[key] = ("submitted", 0.0, token)
                    return None
                if existing and existing[0] == "processing" and existing[1] > now.timestamp():
                    return None
                self._local_claims[key] = ("processing", lease_until.timestamp(), token)
                return ReviewClaim(key, token)

        document = db.collection(self.collection_name).document(key)
        transaction = db.transaction()

        @firestore.transactional
        def transaction_claim(tx: Any) -> bool:
            snapshot = document.get(transaction=tx)
            data = snapshot.to_dict() if snapshot.exists else {}
            if github_review_exists or data.get("status") == "submitted":
                tx.set(document, {"status": "submitted", "updated_at": now}, merge=True)
                return False
            existing_lease = data.get("lease_until")
            if data.get("status") == "processing" and existing_lease and existing_lease > now:
                return False
            tx.set(
                document,
                {
                    "repository": repo_name,
                    "pull_request": pr_number,
                    "head_sha": head_sha,
                    "status": "processing",
                    "claim_token": token,
                    "lease_until": lease_until,
                    "updated_at": now,
                },
            )
            return True

        if transaction_claim(transaction):
            return ReviewClaim(key, token)
        return None

    def mark_submitted(self, claim: ReviewClaim, review_id: str, review_url: str) -> None:
        """Mark a successful GitHub submission as complete."""
        db = self._get_db()
        if db is None:
            with self._local_lock:
                existing = self._local_claims.get(claim.key)
                if existing and existing[2] == claim.token:
                    self._local_claims[claim.key] = ("submitted", 0.0, claim.token)
            return
        document = db.collection(self.collection_name).document(claim.key)
        transaction = db.transaction()

        @firestore.transactional
        def transaction_complete(tx: Any) -> None:
            snapshot = document.get(transaction=tx)
            data = snapshot.to_dict() if snapshot.exists else {}
            if data.get("claim_token") == claim.token:
                tx.set(
                    document,
                    {
                        "status": "submitted",
                        "review_id": review_id,
                        "review_url": review_url,
                        "updated_at": datetime.datetime.now(datetime.UTC),
                    },
                    merge=True,
                )

        transaction_complete(transaction)

    def release(self, claim: ReviewClaim) -> None:
        """Release a failed claim so a later delivery can retry."""
        db = self._get_db()
        if db is None:
            with self._local_lock:
                existing = self._local_claims.get(claim.key)
                if existing and existing[2] == claim.token:
                    self._local_claims.pop(claim.key, None)
            return
        document = db.collection(self.collection_name).document(claim.key)
        transaction = db.transaction()

        @firestore.transactional
        def transaction_release(tx: Any) -> None:
            snapshot = document.get(transaction=tx)
            data = snapshot.to_dict() if snapshot.exists else {}
            if data.get("claim_token") == claim.token and data.get("status") == "processing":
                tx.delete(document)

        transaction_release(transaction)


review_claim_registry = ReviewClaimRegistry()
