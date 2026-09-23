"""Provider seam for single-turn Gemini text generation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from google.genai import Client


@dataclass(frozen=True)
class TextGenerationResult:
    """Normalized result shared by legacy and Interactions providers."""

    text: str
    total_tokens: int = 0
    interaction_id: str | None = None


class TextGenerationProvider(Protocol):
    """Small interface for a single text-generation request."""

    def generate(self, model: str, prompt: str) -> TextGenerationResult:
        """Generate text and normalize provider-specific response fields."""


class GenerateContentProvider:
    """Adapter for the legacy ``generateContent`` endpoint."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def generate(self, model: str, prompt: str) -> TextGenerationResult:
        response = self._client.models.generate_content(model=model, contents=prompt)
        usage_metadata = getattr(response, "usage_metadata", None)
        raw_tokens = (
            getattr(usage_metadata, "total_token_count", 0)
            or getattr(usage_metadata, "total_tokens", 0)
            if usage_metadata
            else 0
        )
        try:
            total_tokens = int(raw_tokens)
        except (TypeError, ValueError):
            total_tokens = 0
        return TextGenerationResult(
            text=getattr(response, "text", "") or "",
            total_tokens=total_tokens,
        )


class InteractionsProvider:
    """Adapter for the single-turn Gemini Interactions API."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def generate(self, model: str, prompt: str) -> TextGenerationResult:
        interaction = self._client.interactions.create(model=model, input=prompt)
        return TextGenerationResult(
            text=getattr(interaction, "output_text", "") or "",
            interaction_id=getattr(interaction, "id", None),
        )


def interactions_enabled() -> bool:
    """Return whether the opt-in Interactions provider is enabled."""
    return os.getenv("GEMINI_API_USE_INTERACTIONS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_text_generation_provider(
    client: Client,
    *,
    use_interactions: bool | None = None,
) -> TextGenerationProvider:
    """Build the configured provider while keeping legacy behavior as default."""
    enabled = interactions_enabled() if use_interactions is None else use_interactions
    if enabled:
        return InteractionsProvider(client)
    return GenerateContentProvider(client)


__all__ = [
    "GenerateContentProvider",
    "InteractionsProvider",
    "TextGenerationProvider",
    "TextGenerationResult",
    "get_text_generation_provider",
    "interactions_enabled",
]
