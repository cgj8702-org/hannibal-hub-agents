"""Bot identity detection helpers for loop-avoidance.

Provides multi-signal detection of events originated by this GitHub App,
used by both the webhook processor and the ADK webhook agent to prevent
infinite feedback loops.
"""

from __future__ import annotations

import os
from typing import Any

# Bot identity constants
BOT_LOGIN = "hannibal-hub-agents[bot]"
BOT_APP_SLUG = "hannibal-hub-agents"


def _is_bot_sender(sender: dict[str, Any] | None) -> bool:
    """Check whether a sender dict represents this app's bot identity.

    Uses multiple signals to avoid false-negatives when GitHub varies
    the sender format across event types or API versions.
    """
    if sender is None:
        return False
    login = sender.get("login", "")
    if login == BOT_LOGIN or login == BOT_APP_SLUG:
        return True
    if sender.get("type") == "Bot" and login.startswith(BOT_APP_SLUG):
        return True
    return False


def _is_bot_event(normalized: dict[str, Any]) -> bool:
    """Determine whether a normalized webhook event originated from this bot.

    Checks the top-level sender, the comment/review author, and the
    ``performed_via_github_app`` field in the raw payload.
    """
    if _is_bot_sender(normalized.get("sender")):
        return True

    raw = normalized.get("raw_payload", {})

    # Check comment / review author
    comment = raw.get("comment") or raw.get("review")
    if comment and _is_bot_sender(comment.get("user")):
        return True

    # Check performed_via_github_app (most reliable signal)
    app_info = raw.get("performed_via_github_app")
    # Also check within comment/review, where GitHub sometimes nests it
    if not app_info and comment:
        app_info = comment.get("performed_via_github_app")

    if app_info:
        if app_info.get("slug") == BOT_APP_SLUG:
            return True
        if str(app_info.get("id", "")) == str(os.environ.get("GITHUB_APP_ID", "")):
            return True

    return False
