"""Transitional backwards-compatibility shim for github_credential_helper.

Relocated to webhook_agent.github.credentials in Phase 6c modularization.
"""

from __future__ import annotations

from webhook_agent.github.credentials import (
    InstallationToken,
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

__all__ = [
    "InstallationToken",
    "generate_jwt",
    "get_installation_token",
    "load_cached_token",
    "load_private_key",
    "save_cached_token",
]
