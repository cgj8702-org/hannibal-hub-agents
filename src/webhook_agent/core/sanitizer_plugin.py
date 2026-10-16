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
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}"),  # GitHub Tokens (ghp, gho, ghu, ghs, ghr)
    re.compile(r"github_pat_[A-Za-z0-9_]{22,255}"),  # GitHub Fine-Grained Personal Access Tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key IDs
    re.compile(r"sk-[A-Za-z0-9]{32,64}"),  # Standard API secret keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_.-]{20,}"),  # Bearer tokens
    re.compile(
        r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END \1-----|$)"
    ),  # PEM Private Keys
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
        """Initialize the prompt sanitizer plugin with a plugin name."""
        super().__init__(name=name)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Intercept and sanitize model output before rendering or downstream processing."""
        if hasattr(llm_response, "content") and llm_response.content:
            if isinstance(llm_response.content, str):
                llm_response.content = sanitize_markdown_text(llm_response.content)
            else:
                parts = getattr(llm_response.content, "parts", None) or []
                for part in parts:
                    if hasattr(part, "text") and isinstance(part.text, str) and part.text:
                        part.text = sanitize_markdown_text(part.text)

        # Return None so ADK continues to the agent-level after_model_callback
        # (which records token usage and normalizes hallucinated tool prefixes).
        # The response was already sanitized in place above.
        return None
