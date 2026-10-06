"""Transitional backwards-compatibility shim for sanitizer_plugin.

Relocated to webhook_agent.core.sanitizer_plugin in Phase 6a modularization.
"""

from __future__ import annotations

from webhook_agent.core.sanitizer_plugin import (
    PROMPT_LEAKAGE_PATTERNS,
    SECRET_PATTERNS,
    PromptSanitizerPlugin,
    sanitize_markdown_text,
)

__all__ = [
    "PROMPT_LEAKAGE_PATTERNS",
    "SECRET_PATTERNS",
    "PromptSanitizerPlugin",
    "sanitize_markdown_text",
]
