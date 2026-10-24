"""Environment variable secret resolver for sensitive credentials.

Resolution order:
1. Environment variable (e.g. os.getenv("WEBHOOK_FREE_KEY")).
"""

from __future__ import annotations

import os


def resolve_secret(secret_id: str, default: str = "") -> str:
    """Resolve a secret strictly from os.getenv with placeholder filtering."""
    val = (os.getenv(secret_id) or "").strip()
    if val and val.lower() not in ("dummy", "dummy-key-for-dev", "none"):
        return val

    return default
