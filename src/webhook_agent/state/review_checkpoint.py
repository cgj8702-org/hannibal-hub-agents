"""Durable PR review checkpointing with 14-day sliding TTL for pause & resume workflows."""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Any

from webhook_agent.constants import DEFAULT_FIRESTORE_PROJECT

logger = logging.getLogger("webhook_agent.logic.review_checkpoint")

firestore: Any = None
try:
    import google.cloud.firestore as _firestore

    firestore = _firestore
    _HAS_FIRESTORE = True
except ImportError:
    _HAS_FIRESTORE = False


def _safe_doc_id(repo: str, pr_number: int, head_sha: str) -> str:
    """Generate a Firestore-safe document ID pinned to repo, PR, and commit SHA."""
    safe_repo = repo.replace("/", "__")
    return f"{safe_repo}__{pr_number}__{head_sha}"


class ReviewCheckpointManager:
    """Manages durable pre-audit dossiers and PR review state across halts and resumptions.

    Enables rate-limited or interrupted reviews to resume directly from the LLM inference
    phase without re-running deterministic AST parsing, symbol graph, or test impact analysis.
    Uses a 14-day sliding TTL on Firestore documents.
    """

    def __init__(
        self, collection_name: str = "review_checkpoints", sliding_ttl_days: int = 14
    ) -> None:
        self.collection_name = collection_name
        self.sliding_ttl_days = sliding_ttl_days
        self._db: Any = None
        self._initialized = False
        self._local_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _get_db(self) -> Any | None:
        if not self._initialized:
            self._initialized = True
            # Opt-in gate: Firestore checkpoint persistence requires
            # ENABLE_REVIEW_CHECKPOINT=1 (load_secrets.sh sets it for the
            # service; .envrc for local dev). IAM prerequisite: roles/datastore.user
            # for webhook-agent-sa in cgj8702-webhook-agent — without it every
            # op throws 403, so default to local memory instead of log spam.
            enabled = os.getenv("ENABLE_REVIEW_CHECKPOINT", "0").lower() in (
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
                    logger.info("Initialized ReviewCheckpointManager in Firestore [%s]", project_id)
                except Exception as exc:
                    logger.warning("Could not initialize Firestore client for checkpoints: %s", exc)
            elif _HAS_FIRESTORE:
                logger.debug(
                    "ReviewCheckpointManager using local memory "
                    "(set ENABLE_REVIEW_CHECKPOINT=1 for Firestore)"
                )
        return self._db

    def get_checkpoint(self, repo: str, pr_number: int, head_sha: str) -> dict[str, Any] | None:
        """Fetch an active checkpoint for the pinned PR commit."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        db = self._get_db()
        if db is not None:
            try:
                doc = db.collection(self.collection_name).document(doc_id).get()
                if doc.exists:
                    data = doc.to_dict() or {}
                    now = datetime.datetime.now(datetime.UTC)
                    expire_at = data.get("expire_at")
                    if expire_at and isinstance(expire_at, datetime.datetime) and expire_at < now:
                        logger.debug("Checkpoint %s expired; ignoring", doc_id)
                        return None
                    return data
            except Exception as exc:
                logger.debug("Checkpoint %s using local fallback (Firestore: %s)", doc_id, exc)

        with self._lock:
            local = self._local_cache.get(doc_id)
            if local:
                now_ts = datetime.datetime.now(datetime.UTC).timestamp()
                if local.get("expire_at_ts", 0) > now_ts:
                    return dict(local)
        return None

    def save_checkpoint(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        canonical: str,
        precompiled_dossier: str,
        pr_diff: str = "",
        changed_files: list[str] | None = None,
        status: str = "pending",
        last_error: str | None = None,
    ) -> bool:
        """Save or update a checkpoint with fresh 14-day sliding TTL."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        now = datetime.datetime.now(datetime.UTC)
        expire_at = now + datetime.timedelta(days=self.sliding_ttl_days)

        payload: dict[str, Any] = {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "canonical": canonical,
            "status": status,
            "precompiled_dossier": precompiled_dossier,
            "pr_diff": pr_diff,
            "changed_files": list(changed_files or []),
            "updated_at": firestore.SERVER_TIMESTAMP if _HAS_FIRESTORE else now.isoformat(),
            "expire_at": expire_at,
            "last_error": last_error,
        }

        # Update local memory
        with self._lock:
            local_entry = dict(payload)
            local_entry["expire_at_ts"] = expire_at.timestamp()
            existing = self._local_cache.get(doc_id)
            local_entry["attempts"] = (existing.get("attempts", 0) + 1) if existing else 1
            self._local_cache[doc_id] = local_entry

        db = self._get_db()
        if db is not None:
            try:
                doc_ref = db.collection(self.collection_name).document(doc_id)
                # Incremental attempts if document exists
                db_payload = dict(payload)
                if _HAS_FIRESTORE and firestore is not None:
                    db_payload["attempts"] = firestore.Increment(1)
                else:
                    db_payload["attempts"] = (existing.get("attempts", 0) + 1) if existing else 1
                doc_ref.set(db_payload, merge=True)
                logger.info(
                    "💾 Saved review checkpoint for %s (status: %s, TTL: 14d)",
                    doc_id,
                    status,
                )
                return True
            except Exception as exc:
                logger.debug(
                    "Checkpoint %s using local fallback on save (Firestore: %s)", doc_id, exc
                )

        return True

    def mark_rate_limited(
        self, repo: str, pr_number: int, head_sha: str, error_message: str
    ) -> bool:
        """Mark checkpoint as paused due to rate limits with updated sliding TTL."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        now = datetime.datetime.now(datetime.UTC)
        expire_at = now + datetime.timedelta(days=self.sliding_ttl_days)

        with self._lock:
            if doc_id in self._local_cache:
                self._local_cache[doc_id]["status"] = "rate_limited"
                self._local_cache[doc_id]["last_error"] = error_message
                self._local_cache[doc_id]["expire_at_ts"] = expire_at.timestamp()

        db = self._get_db()
        if db is not None:
            try:
                timestamp_val = (
                    firestore.SERVER_TIMESTAMP
                    if (_HAS_FIRESTORE and firestore is not None)
                    else now.isoformat()
                )
                db.collection(self.collection_name).document(doc_id).set(
                    {
                        "status": "rate_limited",
                        "last_error": error_message,
                        "updated_at": timestamp_val,
                        "expire_at": expire_at,
                    },
                    merge=True,
                )
                logger.info("⏸️ Checkpoint marked rate_limited for %s", doc_id)
                return True
            except Exception as exc:
                logger.debug(
                    "Checkpoint %s rate_limit using local fallback (Firestore: %s)", doc_id, exc
                )
        return True

    def mark_completed(self, repo: str, pr_number: int, head_sha: str) -> bool:
        """Mark review as successfully completed for this commit SHA."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        with self._lock:
            if doc_id in self._local_cache:
                self._local_cache[doc_id]["status"] = "completed"

        db = self._get_db()
        if db is not None:
            try:
                timestamp_val = (
                    firestore.SERVER_TIMESTAMP
                    if (_HAS_FIRESTORE and firestore is not None)
                    else datetime.datetime.now(datetime.UTC).isoformat()
                )
                db.collection(self.collection_name).document(doc_id).set(
                    {
                        "status": "completed",
                        "updated_at": timestamp_val,
                    },
                    merge=True,
                )
                logger.info("✅ Checkpoint marked completed for %s", doc_id)
                return True
            except Exception as exc:
                logger.debug(
                    "Checkpoint %s completion using local fallback (Firestore: %s)", doc_id, exc
                )
        return True


review_checkpoint_manager = ReviewCheckpointManager()
