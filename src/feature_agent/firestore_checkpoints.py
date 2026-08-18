"""Firestore Checkpoint Registry for Autonomous Feature Engineering Agent.

Manages durable task documents in 'feature_checkpoints/issue_{issue_number}' in Google
Cloud Firestore (FEATURE_AGENT_PROJECT), enabling automatic 429 quota pause & resume.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any

logger = logging.getLogger("feature_agent.firestore")

try:
    from google.cloud import firestore

    _HAS_FIRESTORE = True
except ImportError:
    firestore = None  # type: ignore[assignment]
    _HAS_FIRESTORE = False


class FirestoreFeatureCheckpointRegistry:
    """Tracks task state, Git branch, ADK session ID, and quota pause timestamps."""

    def __init__(self, collection_name: str = "feature_checkpoints") -> None:
        self.collection_name = collection_name
        self._db: Any = None
        self._initialized = False

    def _get_db(self) -> Any | None:
        if not self._initialized:
            self._initialized = True
            if _HAS_FIRESTORE and os.getenv(
                "ENABLE_FIRESTORE_REGISTRY", "1"
            ).lower() in ("1", "true"):
                try:
                    project_id = (
                        os.getenv("FEATURE_AGENT_PROJECT")
                        or os.getenv("GCP_PROJECT_ID")
                        or os.getenv("PUBSUB_PROJECT")
                    )
                    self._db = firestore.Client(project=project_id)
                    logger.info(
                        "🔥 Firestore Feature Checkpoint Registry initialized for project [%s]",
                        project_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not initialize Firestore client for feature checkpoints: %s",
                        exc,
                    )
                    self._db = None
        return self._db

    def save_checkpoint(
        self,
        issue_number: int,
        instruction: str,
        branch_name: str,
        session_id: str,
        status: str,
        last_completed_step: str = "",
        error_msg: str = "",
        pr_url: str = "",
        cooldown_seconds: float = 86400.0,
    ) -> None:
        """Save or update a feature task checkpoint in Firestore."""
        db = self._get_db()
        if db is None:
            return

        doc_id = f"issue_{issue_number}"
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        resume_at = now_utc + datetime.timedelta(seconds=cooldown_seconds)

        data = {
            "issue_number": issue_number,
            "instruction": instruction,
            "branch_name": branch_name,
            "session_id": session_id,
            "status": status,
            "last_completed_step": last_completed_step,
            "error_msg": error_msg,
            "pr_url": pr_url,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if status == "quota_paused":
            data["resume_at"] = resume_at

        try:
            db.collection(self.collection_name).document(doc_id).set(data, merge=True)
            logger.info(
                "🔥 Firestore Feature Checkpoint saved for Issue #%d [Status: %s]",
                issue_number,
                status,
            )
        except Exception as exc:
            logger.warning(
                "Failed to write Firestore feature checkpoint for Issue #%d: %s",
                issue_number,
                exc,
            )

    def get_checkpoint(self, issue_number: int) -> dict[str, Any] | None:
        """Retrieve active feature checkpoint for an issue from Firestore."""
        db = self._get_db()
        if db is None:
            return None

        doc_id = f"issue_{issue_number}"
        try:
            doc = db.collection(self.collection_name).document(doc_id).get()
            if doc.exists:
                return doc.to_dict() or {}
        except Exception as exc:
            logger.debug(
                "Firestore feature checkpoint read skipped for Issue #%d: %s",
                issue_number,
                exc,
            )
        return None


firestore_checkpoint_registry = FirestoreFeatureCheckpointRegistry()
