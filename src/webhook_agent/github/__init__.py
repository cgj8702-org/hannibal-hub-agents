"""GitHub App platform integration, authentication, bot identity, and PR context."""

from __future__ import annotations

from .bot_identity import (
    BOT_APP_SLUG,
    BOT_LOGIN,
    JULES_BOT_LOGINS,
    _is_bot_event,
    _is_bot_sender,
    is_hannibal_bot_login,
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
from .jules_proxy import (
    get_agents_github_client,
    get_agents_github_token,
    handle_jules_pr_opened,
    reopen_jules_pr_as_agent,
)
from .pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
)
from .thread_resolver import (
    ReviewComment,
    ReviewThread,
    auto_resolve_coderabbit_threads,
    fetch_pr_review_threads,
    is_coderabbit_auto_resolve_enabled,
    resolve_review_thread,
)

__all__ = [
    "BOT_APP_SLUG",
    "BOT_LOGIN",
    "JULES_BOT_LOGINS",
    "InstallationToken",
    "ReviewComment",
    "ReviewThread",
    "_is_bot_event",
    "_is_bot_sender",
    "_prefetch_inline_comment_context",
    "_prefetch_pr_diff",
    "_prefetch_previous_bot_reviews",
    "_should_prefetch_diff",
    "auto_resolve_coderabbit_threads",
    "fetch_pr_review_threads",
    "generate_jwt",
    "get_agents_github_client",
    "get_agents_github_token",
    "get_installation_token",
    "handle_jules_pr_opened",
    "is_coderabbit_auto_resolve_enabled",
    "is_hannibal_bot_login",
    "is_jules_sender",
    "load_cached_token",
    "load_private_key",
    "reopen_jules_pr_as_agent",
    "resolve_review_thread",
    "save_cached_token",
]
