"""Backward-compatibility re-export shim for review.comment_poster."""

from webhook_agent.review.comment_poster import (
    build_github_review_comments,
    filter_echo_suggestions,
    format_suggestion_body,
    is_echo_description,
    is_echo_suggestion,
)

__all__ = [
    "build_github_review_comments",
    "filter_echo_suggestions",
    "format_suggestion_body",
    "is_echo_description",
    "is_echo_suggestion",
]
