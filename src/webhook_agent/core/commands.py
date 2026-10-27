"""Backward compatibility alias for webhook_agent.commands."""

from __future__ import annotations

from webhook_agent.commands import (
    REVIEW_INTENT_KEYWORDS,
    REVIEW_SLASH_COMMANDS,
    extract_unquoted_text,
    has_review_slash_command,
)

__all__ = [
    "REVIEW_INTENT_KEYWORDS",
    "REVIEW_SLASH_COMMANDS",
    "extract_unquoted_text",
    "has_review_slash_command",
]
