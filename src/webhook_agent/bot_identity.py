"""Transitional backwards-compatibility shim for bot_identity.

Relocated to webhook_agent.github.bot_identity in Phase 6c modularization.
"""

from __future__ import annotations

from webhook_agent.github.bot_identity import (
    BOT_APP_SLUG,
    BOT_LOGIN,
    JULES_BOT_LOGINS,
    _is_bot_event,
    _is_bot_sender,
    is_jules_sender,
)

__all__ = [
    "BOT_APP_SLUG",
    "BOT_LOGIN",
    "JULES_BOT_LOGINS",
    "_is_bot_event",
    "_is_bot_sender",
    "is_jules_sender",
]
