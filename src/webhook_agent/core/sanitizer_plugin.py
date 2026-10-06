"""Prompt leakage and secret sanitization plugin for Webhook Agent (Google ADK BasePlugin)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from google.adk.plugins import BasePlugin

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),  # Google API Keys
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub Personal Access Tokens
    re.compile(r"gho_[A-Za-z0-9]{36}"),  # GitHub OAuth Tokens
    re.compile(r"sk-[A-Za-z0-9]{32,64}"),  # Standard API secret keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_.-]{20,}"),  # Bearer tokens
]

PROMPT_LEAKAGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"> \[!IMPORTANT\]\s*Finding zero risks.*"),
    re.compile(r"(?i)As an AI model instructed to find zero risks.*"),
    re.compile(r"(?i)System Instruction:.*"),
    re.compile(r"(?i)User Instruction:.*"),
]


def sanitize_markdown_text(text: str) -> str:
    """Sanitize LLM output text by removing prompt leakage and redacting secrets."""
    if not text:
        return text

    sanitized = text

    # Redact secret patterns
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)

    # Strip prompt leakage patterns
    for pattern in PROMPT_LEAKAGE_PATTERNS:
        sanitized = pattern.sub("", sanitized)

    return sanitized.strip()


class PromptSanitizerPlugin(BasePlugin):
    """ADK Plugin intercepting model responses to strip prompt leakage and secrets."""

    def __init__(self, name: str = "prompt_sanitizer_plugin") -> None:
        super().__init__(name=name)

    async def after_model_callback(
        self, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Intercept and sanitize model output before rendering or downstream processing."""
        if hasattr(llm_response, "content") and llm_response.content:
            if isinstance(llm_response.content, str):
                llm_response.content = sanitize_markdown_text(llm_response.content)

        return llm_response
