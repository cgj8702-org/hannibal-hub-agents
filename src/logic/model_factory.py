"""Centralized Google ADK Model Factory.

Provides RateLimitedGemini wrapper and get_adk_model factory function
to unify model instantiation, rate limiting, and API key handling across agents.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import Gemini

try:
    from logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        rpm_waiter,
    )
except ImportError:
    from src.logic.rate_limiter import (
        _resolve_tier,
        get_active_api_key,
        rpm_waiter,
    )

logger = logging.getLogger("logic.model_factory")


class RateLimitedGemini(Gemini):
    """Production-grade Rate-Limited Gemini wrapper for ADK agents."""

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[Any, None]:
        from webhook_agent.webhook_agent import get_active_model

        model_name = getattr(
            llm_request, "model", getattr(self, "model", get_active_model())
        )
        active_tier = _resolve_tier()
        estimated_tokens = 0

        try:
            if self.api_client:
                ct_resp = await self.api_client.aio.models.count_tokens(
                    model=model_name,
                    contents=llm_request.contents,
                )
                if ct_resp and ct_resp.total_tokens:
                    estimated_tokens = int(ct_resp.total_tokens)
        except Exception as exc:
            logger.debug(
                "Free count_tokens API call skipped on model '%s': %s", model_name, exc
            )

        if estimated_tokens <= 0:
            contents_str = str(getattr(llm_request, "contents", ""))
            estimated_tokens = max(1, len(contents_str) // 4)

        try:
            await rpm_waiter.check_and_wait(
                model=model_name,
                estimated_tokens=estimated_tokens,
                tier=active_tier,
            )
        except Exception as exc:
            logger.warning(
                "RPM/TPM pre-flight check error on model '%s' (tier '%s'): %s",
                model_name,
                active_tier,
                exc,
            )

        async for response in super().generate_content_async(
            llm_request, stream=stream
        ):
            yield response


def get_adk_model(
    model_name: str | None = None,
    api_key: str | None = None,
    tier: str | None = None,
    client_kwargs: dict[str, Any] | None = None,
    rate_limited: bool = True,
) -> Gemini:
    """Construct a standardized ADK Gemini model instance.

    Args:
        model_name: Name of the model (defaults to GEMMA_MODEL / active tier model).
        api_key: Optional explicit API key override. Defaults to active tier key.
        tier: Optional explicit tier override ("free" or "paid").
        client_kwargs: Additional kwargs passed to Google GenAI client constructor.
        rate_limited: If True, uses RateLimitedGemini wrapper.

    Returns:
        Configured Gemini model instance.
    """
    active_tier = tier or _resolve_tier()

    if not model_name:
        default_model = (
            "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.6-flash"
        )
        model_name = os.getenv("GEMMA_MODEL", default_model)

    resolved_api_key = api_key or get_active_api_key()
    final_client_kwargs = dict(client_kwargs or {})
    if resolved_api_key and "api_key" not in final_client_kwargs:
        final_client_kwargs["api_key"] = resolved_api_key

    model_cls = RateLimitedGemini if rate_limited else Gemini

    return model_cls(
        model=model_name,
        client_kwargs=final_client_kwargs if final_client_kwargs else None,
    )
