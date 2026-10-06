"""Durable PR review checkpointing with 14-day sliding TTL for pause & resume workflows."""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Any

from webhook_agent.constants import DEFAULT_FIRESTORE_PROJECT

logger = logging.getLogger("webhook_agent.state.review_checkpoint")

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


MAX_CHECKPOINT_DIFF_BYTES = 400 * 1024  # 400 KiB max for PR diff
MAX_CHECKPOINT_DOSSIER_BYTES = 500 * 1024  # 500 KiB max for precompiled dossier


def _guard_payload_size(text: str, max_bytes: int, label: str) -> str:
    """Ensure string payload does not exceed max_bytes for Firestore 1MiB document limit."""
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return (
        f"{truncated}\n\n[TRUNCATED: {label} exceeded {max_bytes // 1024} KiB document size guard]"
    )


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
        precompiled_dossier: str = "",
        pr_diff: str = "",
        changed_files: list[str] | None = None,
        status: str = "pending",
        last_error: str | None = None,
        delivery_id: str | None = None,
        sender: str | None = None,
        comment_body: str | None = None,
        installation_id: int | str | None = None,
        resume_after: datetime.datetime | None = None,
    ) -> bool:
        """Save or update a checkpoint with fresh 14-day sliding TTL and payload size guards."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        now = datetime.datetime.now(datetime.UTC)
        expire_at = now + datetime.timedelta(days=self.sliding_ttl_days)

        # Apply document size guard for Firestore 1 MiB limit
        safe_dossier = _guard_payload_size(
            precompiled_dossier, MAX_CHECKPOINT_DOSSIER_BYTES, "Precompiled dossier"
        )
        safe_diff = _guard_payload_size(pr_diff, MAX_CHECKPOINT_DIFF_BYTES, "PR diff")

        # Never downgrade status: preserve existing completed or rate_limited status
        with self._lock:
            existing = self._local_cache.get(doc_id)
            if (
                existing
                and existing.get("status") in ("completed", "rate_limited")
                and status == "pending"
            ):
                status = str(existing.get("status"))

        payload: dict[str, Any] = {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "canonical": canonical,
            "status": status,
            "precompiled_dossier": safe_dossier,
            "pr_diff": safe_diff,
            "changed_files": list(changed_files or []),
            "updated_at": firestore.SERVER_TIMESTAMP if _HAS_FIRESTORE else now.isoformat(),
            "expire_at": expire_at,
            "last_error": last_error,
            "delivery_id": delivery_id or "",
            "sender": sender or "",
            "comment_body": comment_body or "",
            "installation_id": installation_id,
        }
        if resume_after is not None:
            payload["resume_after"] = resume_after

        # Update local memory
        with self._lock:
            local_entry = dict(payload)
            local_entry["expire_at_ts"] = expire_at.timestamp()
            if resume_after is not None:
                local_entry["resume_after_ts"] = resume_after.timestamp()
            elif existing and "resume_after_ts" in existing:
                local_entry["resume_after_ts"] = existing["resume_after_ts"]
                local_entry["resume_after"] = existing.get("resume_after")
            local_entry["attempts"] = (existing.get("attempts", 0) + 1) if existing else 1
            self._local_cache[doc_id] = local_entry

        db = self._get_db()
        if db is not None:
            try:
                doc_ref = db.collection(self.collection_name).document(doc_id)
                # Check Firestore document status to never downgrade in remote store
                try:
                    existing_doc = doc_ref.get()
                    if existing_doc.exists:
                        remote_data = existing_doc.to_dict() or {}
                        remote_status = remote_data.get("status")
                        if (
                            remote_status in ("completed", "rate_limited")
                            and payload["status"] == "pending"
                        ):
                            payload["status"] = remote_status
                except Exception:
                    pass

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
                    payload["status"],
                )
                return True
            except Exception as exc:
                logger.debug(
                    "Checkpoint %s using local fallback on save (Firestore: %s)", doc_id, exc
                )

        return True

    def mark_rate_limited(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        error_message: str,
        retry_after_seconds: float | None = None,
    ) -> bool:
        """Mark checkpoint as paused due to rate limits with resume_after timestamp and updated sliding TTL."""
        doc_id = _safe_doc_id(repo, pr_number, head_sha)
        now = datetime.datetime.now(datetime.UTC)
        expire_at = now + datetime.timedelta(days=self.sliding_ttl_days)

        backoff_seconds = (
            float(retry_after_seconds)
            if (retry_after_seconds is not None and float(retry_after_seconds) > 0)
            else 60.0
        )
        resume_after = now + datetime.timedelta(seconds=backoff_seconds)

        with self._lock:
            if doc_id in self._local_cache:
                self._local_cache[doc_id]["status"] = "rate_limited"
                self._local_cache[doc_id]["last_error"] = error_message
                self._local_cache[doc_id]["expire_at_ts"] = expire_at.timestamp()
                self._local_cache[doc_id]["resume_after_ts"] = resume_after.timestamp()
                self._local_cache[doc_id]["resume_after"] = resume_after

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
                        "resume_after": resume_after,
                    },
                    merge=True,
                )
                logger.info(
                    "⏸️ Checkpoint marked rate_limited for %s (resume_after: %s, delay: %.1fs)",
                    doc_id,
                    resume_after.isoformat(),
                    backoff_seconds,
                )
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
