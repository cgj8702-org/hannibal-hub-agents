"""Centralized Google ADK Model Factory.

Provides RateLimitedGemini wrapper and get_adk_model factory function
to unify model instantiation, rate limiting, and API key handling across agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models import Gemini

from webhook_agent.constants import (
    DEFAULT_MODEL_CALL_ATTEMPTS,
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
)
from webhook_agent.models.rate_limiter import (
    _resolve_tier,
    get_active_api_key,
    rpm_waiter,
)

logger = logging.getLogger("webhook_agent.models.model_factory")

TIMEOUT_RETRIES_METADATA_KEY = "timeout_retries"


def _get_model_call_timeout_seconds() -> float | None:
    """Resolve per-attempt model call timeout in seconds from env (<= 0 disables)."""
    raw = os.getenv("MODEL_CALL_TIMEOUT_SECONDS", DEFAULT_MODEL_CALL_TIMEOUT_SECONDS).strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid MODEL_CALL_TIMEOUT_SECONDS=%r; falling back to %ss",
            raw,
            DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_MODEL_CALL_TIMEOUT_SECONDS)
    if not math.isfinite(value):
        logger.warning(
            "Non-finite MODEL_CALL_TIMEOUT_SECONDS=%r; falling back to %ss",
            raw,
            DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_MODEL_CALL_TIMEOUT_SECONDS)
    return value if value > 0 else None


def _get_model_call_attempts() -> int:
    """Resolve max attempts per model call on timeout from env (clamped to >= 1)."""
    raw = os.getenv("MODEL_CALL_ATTEMPTS", DEFAULT_MODEL_CALL_ATTEMPTS).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "Invalid MODEL_CALL_ATTEMPTS=%r; falling back to %s",
            raw,
            DEFAULT_MODEL_CALL_ATTEMPTS,
        )
        return int(DEFAULT_MODEL_CALL_ATTEMPTS)


async def _call_model_with_timeout_retry(
    generate: Callable[[], AsyncGenerator[Any]],
    *,
    timeout_seconds: float | None,
    max_attempts: int,
    model_name: str,
) -> list[Any]:
    """Run a non-streaming model call, retrying attempts that time out.

    Each attempt is bounded by `timeout_seconds` (when positive) and wrapped in
    `contextlib.aclosing()` so a stalled underlying HTTP stream is closed before
    a retry begins. Responses are buffered per attempt so a retry never repeats
    a previously yielded response.

    Args:
        generate: Zero-argument factory starting one model call async generator.
        timeout_seconds: Per-attempt timeout in seconds, or None/<=0 to disable.
        max_attempts: Total number of attempts before propagating TimeoutError.
        model_name: Model name for diagnostic warning logs.

    Returns:
        Buffered responses from the first attempt that finished in time. After a
        timeout retry, each response supporting ``custom_metadata`` records the
        number of timed-out attempts under `TIMEOUT_RETRIES_METADATA_KEY`.

    Raises:
        TimeoutError: If the final attempt times out.
    """
    attempts = max(1, max_attempts)
    effective_timeout = (
        timeout_seconds if (timeout_seconds is not None and timeout_seconds > 0) else None
    )
    retries = 0
    responses: list[Any] = []
    for attempt_idx in range(attempts):
        is_last = attempt_idx + 1 >= attempts
        try:
            async with (
                asyncio.timeout(effective_timeout),
                contextlib.aclosing(generate()) as attempt,
            ):
                responses = [response async for response in attempt]
            break
        except TimeoutError as exc:
            if is_last:
                raise
            retries += 1
            logger.warning(
                "Model '%s' call attempt %d of %d timed out after %ss (%s); retrying",
                model_name,
                retries,
                attempts,
                effective_timeout,
                type(exc).__name__,
            )
    if retries:
        for response in responses:
            if hasattr(response, "custom_metadata"):
                existing = getattr(response, "custom_metadata", None) or {}
                response.custom_metadata = {
                    **existing,
                    TIMEOUT_RETRIES_METADATA_KEY: retries,
                }
    return responses


class _SuppressApiKeyWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" not in record.getMessage()


logging.getLogger("google_genai._api_client").addFilter(_SuppressApiKeyWarningFilter())


class RateLimitedGemini(Gemini):
    """Production-grade Rate-Limited Gemini wrapper for ADK agents."""

    call_timeout_seconds: float | None = None
    call_attempts: int | None = None

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[Any]:
        from webhook_agent.models.model_chain import get_active_model

        model_name = getattr(llm_request, "model", getattr(self, "model", get_active_model()))
        active_tier = _resolve_tier()
        if not getattr(llm_request, "_rate_limit_checked", False):
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

        if self.call_timeout_seconds is not None:
            timeout_seconds: float | None = (
                self.call_timeout_seconds if self.call_timeout_seconds > 0 else None
            )
        else:
            timeout_seconds = _get_model_call_timeout_seconds()
        max_attempts = (
            max(1, self.call_attempts)
            if self.call_attempts is not None
            else _get_model_call_attempts()
        )
        parent_generate = super().generate_content_async

        yielded_any = False
        try:
            if not stream:
                responses = await _call_model_with_timeout_retry(
                    lambda: parent_generate(llm_request, stream=False),
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    model_name=model_name,
                )
                for response in responses:
                    yielded_any = True
                    yield response
                return

            for attempt_idx in range(max_attempts):
                is_last = attempt_idx + 1 >= max_attempts
                try:
                    async with (
                        asyncio.timeout(timeout_seconds),
                        contextlib.aclosing(parent_generate(llm_request, stream=True)) as attempt,
                    ):
                        async for response in attempt:
                            yielded_any = True
                            yield response
                    return
                except TimeoutError as exc:
                    if yielded_any or is_last:
                        raise
                    logger.warning(
                        "Model '%s' streaming call attempt %d of %d timed out after %ss (%s); retrying",
                        model_name,
                        attempt_idx + 1,
                        max_attempts,
                        timeout_seconds,
                        type(exc).__name__,
                    )
            return
        except Exception as exc:
            if yielded_any:
                # Output already streamed to the caller; propagate untouched.
                raise

            err_s = str(exc).lower()
            if "429" in err_s or "resource_exhausted" in err_s or "quota exceeded" in err_s:
                try:
                    from webhook_agent.models.depleted_registry import (
                        firestore_depleted_registry,
                    )

                    firestore_depleted_registry.mark_depleted(model_name, error=exc)
                except Exception as dep_err:
                    logger.debug("Failed to mark model '%s' as depleted: %s", model_name, dep_err)

            # Fail fast and propagate transparently: the orchestrator in
            # agent_definition is the single retry/failover and logging boundary
            # for transient Google API errors. Re-raising without logging here
            # keeps one Google error from being printed once per retry layer.
            raise


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
    if not model_name:
        default_model = "gemini-3.5-flash-lite"
        model_name = os.getenv("PRIMARY_MODEL", os.getenv("GEMMA_MODEL", default_model))

    resolved_api_key = api_key or get_active_api_key()
    final_client_kwargs = dict(client_kwargs or {})
    if resolved_api_key and "api_key" not in final_client_kwargs:
        final_client_kwargs["api_key"] = resolved_api_key

    model_cls = RateLimitedGemini if rate_limited else Gemini

    return model_cls(
        model=model_name,
        client_kwargs=final_client_kwargs if final_client_kwargs else None,
    )
