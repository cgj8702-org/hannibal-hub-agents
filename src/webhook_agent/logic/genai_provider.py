"""Provider seam for single-turn Gemini text generation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.genai import Client


def _normalize_text(value: Any) -> str:
    """Coerce provider payloads to a plain string without blowing up on mocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = str(value)
    return text if text != "<MagicMock name='" else ""


def _extract_citations(response: Any) -> list[str]:
    """Collect search citations from either generateContent or Interactions responses."""
    citations: list[str] = []
    seen: set[str] = set()

    def add_citation(title: Any, uri: Any) -> None:
        if not uri:
            return
        uri_text = str(uri)
        if not uri_text or uri_text in seen:
            return
        seen.add(uri_text)
        title_text = _normalize_text(title) or "Source"
        citations.append(f"- [{title_text}]({uri_text})")

    candidate_list = getattr(response, "candidates", None) or []
    for candidate in candidate_list:
        grounding_meta = getattr(candidate, "grounding_metadata", None)
        chunks = getattr(grounding_meta, "grounding_chunks", None) or []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            add_citation(getattr(web, "title", None), getattr(web, "uri", None))

    steps = getattr(response, "steps", None) or []
    for step in steps:
        results = getattr(step, "result", None) or []
        if isinstance(results, dict):
            results = [results]
        for result in results:
            if isinstance(result, dict):
                uri = result.get("uri") or result.get("url")
                title = result.get("title") or result.get("name") or "Source"
                add_citation(title, uri)
                continue
            uri = getattr(result, "uri", None) or getattr(result, "url", None)
            title = getattr(result, "title", None) or getattr(result, "name", None) or "Source"
            add_citation(title, uri)

    return citations


@dataclass(frozen=True)
class TextGenerationResult:
    """Normalized result shared by legacy and Interactions providers."""

    text: str
    total_tokens: int = 0
    interaction_id: str | None = None
    citations: list[str] = field(default_factory=list)


class TextGenerationProvider(Protocol):
    """Small interface for a single text-generation request."""

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        config: Any | None = None,
        **kwargs: Any,
    ) -> TextGenerationResult:
        """Generate text and normalize provider-specific response fields."""


class GenerateContentProvider:
    """Adapter for the legacy ``generateContent`` endpoint."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        config: Any | None = None,
        **kwargs: Any,
    ) -> TextGenerationResult:
        call_kwargs: dict[str, Any] = {"model": model, "contents": prompt}
        if config is not None:
            call_kwargs["config"] = config
        call_kwargs.update(kwargs)
        response = self._client.models.generate_content(**call_kwargs)
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
            text=_normalize_text(getattr(response, "text", "") or ""),
            total_tokens=total_tokens,
            citations=_extract_citations(response),
        )


class InteractionsProvider:
    """Adapter for the single-turn Gemini Interactions API."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        config: Any | None = None,
        **kwargs: Any,
    ) -> TextGenerationResult:
        call_kwargs: dict[str, Any] = {"model": model, "input": prompt}
        if config is not None:
            call_kwargs["config"] = config
        call_kwargs.update(kwargs)
        interaction = self._client.interactions.create(**call_kwargs)
        return TextGenerationResult(
            text=_normalize_text(getattr(interaction, "output_text", "") or ""),
            interaction_id=getattr(interaction, "id", None),
            citations=_extract_citations(interaction),
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
