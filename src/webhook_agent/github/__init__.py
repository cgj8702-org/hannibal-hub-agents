"""GitHub App platform integration, authentication, bot identity, and PR context."""

from __future__ import annotations

from .bot_identity import (
    BOT_APP_SLUG,
    BOT_LOGIN,
    JULES_BOT_LOGINS,
    _is_bot_event,
    _is_bot_sender,
    is_jules_sender,
)
from .credentials import (
    InstallationToken,
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)
from .pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
)

__all__ = [
    "BOT_APP_SLUG",
    "BOT_LOGIN",
    "JULES_BOT_LOGINS",
    "InstallationToken",
    "_is_bot_event",
    "_is_bot_sender",
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_should_prefetch_diff",
    "generate_jwt",
    "get_installation_token",
    "is_jules_sender",
    "load_cached_token",
    "load_private_key",
    "save_cached_token",
]
