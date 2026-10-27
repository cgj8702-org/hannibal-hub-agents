"""Command definitions and parsing for webhook agent interactions."""

from __future__ import annotations

import re

REVIEW_SLASH_COMMANDS: tuple[str, ...] = (
    "/review",
    "/re-review",
    "/rereview",
    "/audit",
    "/force-review",
    "/force_review",
)

# Kept for backward compatibility with imports expecting REVIEW_INTENT_KEYWORDS
REVIEW_INTENT_KEYWORDS: tuple[str, ...] = REVIEW_SLASH_COMMANDS


def extract_unquoted_text(body: str) -> str:
    """Return text with markdown blockquote lines stripped."""
    if not body:
        return ""
    clean_lines = [
        line for line in body.splitlines() if not line.strip().startswith((">", "│", "»"))
    ]
    return "\n".join(clean_lines)


def has_review_slash_command(text: str) -> bool:
    """Check if unquoted text contains any recognized review slash command."""
    unquoted = extract_unquoted_text(text).lower()
    for cmd in REVIEW_SLASH_COMMANDS:
        pattern = rf"(?<!\S){re.escape(cmd)}\b"
        if re.search(pattern, unquoted):
            return True
    return False
