"""Firestore-backed Depleted Model Registry for cross-process 429 quota depletion tracking.

Uses Google Cloud Firestore for persistent depletion state and TTL auto-purging,
with graceful fallback to in-memory tracking when Firestore is unavailable.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Any

logger = logging.getLogger("firestore_registry")

try:
    from google.cloud import firestore

    _HAS_FIRESTORE = True
except ImportError:
    firestore = None  # type: ignore[assignment]
    _HAS_FIRESTORE = False


class FirestoreDepletedModelRegistry:
    """Tracks models and API key pairs that hit 429 quota exhaustion.

    Stores depletion state in Firestore collection 'depleted_models' with an 'expire_at'
    timestamp field for Firestore native TTL auto-deletion, falling back to local memory.
    """

    def __init__(
        self,
        collection_name: str = "depleted_models",
        default_cooldown: float = 3600.0,
    ) -> None:
        self.collection_name = collection_name
        self.default_cooldown = default_cooldown
        self._local_depleted: dict[str, tuple[float, float]] = {}
        self._db: Any = None
        self._initialized = False

    def _get_db(self) -> Any | None:
        if not self._initialized:
            self._initialized = True
            if _HAS_FIRESTORE and os.getenv(
                "ENABLE_FIRESTORE_REGISTRY", "0"
            ).lower() in ("1", "true"):
                try:
                    project_id = os.getenv("GCP_PROJECT_ID") or os.getenv(
                        "PUBSUB_PROJECT"
                    )
                    self._db = firestore.Client(project=project_id)
                    logger.info(
                        "🔥 Firestore Depleted Model Registry initialized for project [%s]",
                        project_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not initialize Firestore client: %s. Using local memory.",
                        exc,
                    )
                    self._db = None
        return self._db

    def mark_depleted(
        self, model_name: str, error: Exception | None = None, key_alias: str = ""
    ) -> None:
        """Mark a model (and optional key alias) as depleted across memory and Firestore."""
        from logic.rate_limiter import extract_rate_limit_details

        cooldown = self.default_cooldown
        metric_type = "DEFAULT (1h)"

        if error is not None:
            details = extract_rate_limit_details(error)
            retry_after = details.get("retry_after_seconds")
            quota_limit = details.get("quota_limit") or ""

            if isinstance(retry_after, (int, float)) and retry_after > 0:
                cooldown = float(retry_after)
                metric_type = f"EXACT ({cooldown:.1f}s)"
            elif "perday" in quota_limit.lower() or "perday" in str(error).lower():
                cooldown = 86400.0  # 24 Hours for RPD daily limit
                metric_type = "RPD (24h)"
            elif (
                "perminute" in quota_limit.lower()
                or "perminute" in str(error).lower()
                or "tokensperminute" in str(error).lower()
            ):
                cooldown = 60.0  # 60 Seconds for RPM/TPM minute limit
                metric_type = "RPM/TPM (60s)"

        doc_id = f"{key_alias}_{model_name}" if key_alias else model_name
        now = time.time()
        self._local_depleted[doc_id] = (now, cooldown)
        logger.warning("🔴 Model '%s' marked DEPLETED [%s]", doc_id, metric_type)

        db = self._get_db()
        if db is not None:
            try:
                expire_dt = datetime.datetime.now(
                    datetime.timezone.utc
                ) + datetime.timedelta(seconds=cooldown)
                db.collection(self.collection_name).document(doc_id).set(
                    {
                        "model": model_name,
                        "key_alias": key_alias,
                        "depleted_at": firestore.SERVER_TIMESTAMP,
                        "cooldown_seconds": cooldown,
                        "expire_at": expire_dt,
                        "metric_type": metric_type,
                    }
                )
                logger.info(
                    "🔥 Persisted depletion record for '%s' to Firestore (TTL: %s)",
                    doc_id,
                    expire_dt,
                )
            except Exception as exc:
                logger.warning("Failed to write depletion to Firestore: %s", exc)

    def is_depleted(self, model_name: str, key_alias: str = "") -> bool:
        """Check whether a model/key pair is currently depleted."""
        doc_id = f"{key_alias}_{model_name}" if key_alias else model_name
        now = time.time()

        # Check local memory first
        if doc_id in self._local_depleted:
            ts, cooldown = self._local_depleted[doc_id]
            if now - ts <= cooldown:
                return True
            del self._local_depleted[doc_id]
            logger.info("🟢 Local depletion cooldown expired for '%s'", doc_id)

        # Fallback check Firestore if enabled
        db = self._get_db()
        if db is not None:
            try:
                doc = db.collection(self.collection_name).document(doc_id).get()
                if doc.exists:
                    data = doc.to_dict() or {}
                    expire_at = data.get("expire_at")
                    if expire_at and isinstance(expire_at, datetime.datetime):
                        if expire_at.tzinfo is None:
                            expire_at = expire_at.replace(tzinfo=datetime.timezone.utc)
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        if expire_at > now_utc:
                            remaining = (expire_at - now_utc).total_seconds()
                            self._local_depleted[doc_id] = (now, remaining)
                            return True
                    return False
            except Exception as exc:
                logger.debug("Firestore depletion read check skipped: %s", exc)

        return False

    @property
    def _depleted(self) -> dict[str, tuple[float, float]]:
        return self._local_depleted

    def clear(self) -> None:
        """Clear local depleted memory cache."""
        self._local_depleted.clear()

    def filter_chain(self, chain: list[str], key_alias: str = "") -> list[str]:
        """Filter out models currently marked as depleted."""
        return [m for m in chain if not self.is_depleted(m, key_alias=key_alias)]


firestore_depleted_registry = FirestoreDepletedModelRegistry()
