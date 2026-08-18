"""GCP Secret Manager fallback resolver for sensitive credentials.

Resolution order:
1. Environment variable (e.g. os.getenv("WEBHOOK_FREE_KEY")).
2. Secret Manager payload from project 'cgj8702-webhook-agent'.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("logic.secret_manager")

_SECRET_CACHE: dict[str, str] = {}


def resolve_secret(secret_id: str, default: str = "") -> str:
    """Resolve a secret strictly from os.getenv first, with GCP Secret Manager fallback."""
    val = (os.getenv(secret_id) or "").strip()
    if val and val.lower() not in ("dummy", "dummy-key-for-dev", "none"):
        return val

    if secret_id in _SECRET_CACHE:
        return _SECRET_CACHE[secret_id]

    # Attempt resolution from Secret Manager (cgj8702-webhook-agent)
    try:
        from google.cloud import secretmanager

        project_id = os.getenv("WEBHOOK_PAID_PROJECT", "cgj8702-webhook-agent")
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_val = response.payload.data.decode("utf-8").strip()
        if secret_val:
            _SECRET_CACHE[secret_id] = secret_val
            logger.debug(
                "Successfully resolved secret '%s' from Secret Manager", secret_id
            )
            return secret_val
    except Exception as exc:
        logger.debug("Secret Manager fallback skipped for '%s': %s", secret_id, exc)

    return default
