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
    if not isinstance(sender, dict):
        return False
    login = (sender.get("login") or "").strip().lower()
    sender_type = (sender.get("type") or "").strip()

    known_bot_logins = {
        BOT_LOGIN.lower(),
        BOT_APP_SLUG.lower(),
        "hannibal-hub-agents[bot]",
        "hannibal-hub-agents",
        "github-actions[bot]",
    }
    if login in known_bot_logins:
        return True

    if login.endswith("[bot]") and ("hannibal" in login or "agent" in login):
        return True

    if sender_type == "Bot" and ("hannibal" in login or "agent" in login):
        return True

    return False


def _is_bot_event(normalized: dict[str, Any]) -> bool:
    """Determine whether a normalized webhook event originated from this bot.

    Checks top-level sender, raw_payload sender, comment author, review author,
    and performed_via_github_app.
    """
    # 1. Top-level sender check
    if _is_bot_sender(normalized.get("sender")):
        return True

    raw = normalized.get("raw_payload")
    if not isinstance(raw, dict):
        return False

    # 2. Raw payload sender check
    if _is_bot_sender(raw.get("sender")):
        return True

    # 3. Comment author check
    comment = raw.get("comment")
    if isinstance(comment, dict) and _is_bot_sender(comment.get("user")):
        return True

    # 4. Review author check
    review = raw.get("review")
    if isinstance(review, dict) and _is_bot_sender(review.get("user")):
        return True

    # 5. Review comment author check
    review_comment = raw.get("review_comment")
    if isinstance(review_comment, dict) and _is_bot_sender(review_comment.get("user")):
        return True

    # 6. Check performed_via_github_app (most reliable signal)
    app_info = raw.get("performed_via_github_app")
    if not app_info and isinstance(comment, dict):
        app_info = comment.get("performed_via_github_app")
    if not app_info and isinstance(review, dict):
        app_info = review.get("performed_via_github_app")
    if not app_info and isinstance(review_comment, dict):
        app_info = review_comment.get("performed_via_github_app")

    if isinstance(app_info, dict):
        slug = (app_info.get("slug") or "").lower()
        if slug in (BOT_APP_SLUG.lower(), "hannibal-hub-agents"):
            return True
        app_id_env = str(os.environ.get("GITHUB_APP_ID", "")).strip()
        if app_id_env and str(app_info.get("id", "")).strip() == app_id_env:
            return True

    return False
