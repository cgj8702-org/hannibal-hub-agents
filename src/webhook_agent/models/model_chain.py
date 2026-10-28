"""Model chain routing, capacity ordering, dynamic event selection, and transient error detection.

Extracted from webhook_agent.py as Phase 1 of codebase modularization.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.genai.errors import ServerError as GenAIServerError

from webhook_agent.models.depleted_registry import (
    FirestoreDepletedModelRegistry as DepletedModelRegistry,
)
from webhook_agent.models.depleted_registry import (
    firestore_depleted_registry as _DEPLETED_MODEL_REGISTRY,
)
from webhook_agent.models.rate_limiter import (
    _resolve_tier,
    get_active_api_key,
)

logger = logging.getLogger("webhook_agent.models.model_chain")

__all__ = [
    "_DEPLETED_MODEL_REGISTRY",
    "DepletedModelRegistry",
    "get_active_model",
    "get_model_chain",
    "is_gemini_3_plus",
]

# ---------------------------------------------------------------------------
# Chain Resolution & Event Selection
# ---------------------------------------------------------------------------

FREE_TIER_DEFAULT_CHAIN: list[str] = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

PAID_TIER_DEFAULT_CHAIN: list[str] = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]

# Heavy payload threshold for 4M TPM high-throughput routing
PAYLOAD_HEAVY_THRESHOLD_TOKENS = 50000

# Gemma Free Tier input tokens ceiling (hard quota: 16k TPM; 10k gives a safety buffer)
GEMMA_FREE_TIER_MAX_INPUT_TOKENS = 10000

# Escalation keywords for deep reasoning / audit fallback
DEEP_REASONING_KEYWORDS: tuple[str, ...] = (
    "/deep",
    "/audit",
    "/reason",
)


def get_model_chain(estimated_tokens: int | None = None) -> list[str]:
    """Build ordered list of fallback models sorted by capacity, TPM bandwidth, and tier.

    Filters out models currently marked as depleted in _DEPLETED_MODEL_REGISTRY.
    See docs/MODEL_CHAIN.md for the canonical reference.

    Args:
        estimated_tokens: Optional token estimate of the prompt/payload.
            If provided and > GEMMA_FREE_TIER_MAX_INPUT_TOKENS (10k tokens),
            Gemma models are excluded to prevent 429 RESOURCE_EXHAUSTED TPM blocks.

    Free Tier Chain (250k TPM High-Bandwidth):
        1. gemini-3.5-flash-lite (500 RPD / 250k TPM)
        2. gemini-3.1-flash-lite (500 RPD / 250k TPM)
        3. gemini-3.8-flash (20 RPD / 250k TPM)
        4. gemini-3.7-flash (20 RPD / 250k TPM)
        5. gemini-3.6-flash (20 RPD / 250k TPM)
        6. gemini-3.5-flash (20 RPD / 250k TPM)
        7. gemini-2.5-flash-lite (20 RPD / 250k TPM)
        8. gemini-2.5-flash (20 RPD / 250k TPM)
        9. gemma-4-31b-it (14,400 RPD / 16k TPM, token-gated <= 10k)
        10. gemma-4-26b-a4b-it (14,400 RPD / 16k TPM, token-gated <= 10k)

    Paid Tier Chain (3.x High-Bandwidth & 4M TPM Optimized):
        1. gemini-3.5-flash-lite (150,000 RPD / 4M TPM)
        2. gemini-3.1-flash-lite (150,000 RPD / 4M TPM)
        3. gemini-3.8-flash (10,000 RPD / 2M TPM) - Secondary reasoning fallback ($1.50/1M in)
        4. gemini-3.7-flash (10,000 RPD / 2M TPM)
        5. gemini-3.6-flash (10,000 RPD / 2M TPM)
        6. gemini-3.5-flash (10,000 RPD / 2M TPM)
        7. gemini-2.5-pro (1,000 RPD / 2M TPM) - High-end reasoning escalation ($1.25/1M in)
        8. gemini-2.5-flash (10,000 RPD / 1M TPM)
    """
    active_tier = _resolve_tier()
    default_primary = "gemini-3.1-flash-lite"
    if active_tier == "paid":
        default_chain = list(PAID_TIER_DEFAULT_CHAIN)
    else:
        default_chain = list(FREE_TIER_DEFAULT_CHAIN)
        if estimated_tokens is None or estimated_tokens <= GEMMA_FREE_TIER_MAX_INPUT_TOKENS:
            default_chain.extend(
                [
                    os.environ.get("GEMMA_HEAVY_MODEL", "gemma-4-31b-it"),
                    os.environ.get("GEMMA_FALLBACK_MODEL", "gemma-4-26b-a4b-it"),
                ]
            )

    # Support PRIMARY_MODEL with fallback to legacy GEMMA_MODEL
    raw_primary = os.environ.get(
        "PRIMARY_MODEL",
        os.environ.get("GEMMA_MODEL", default_primary),
    )
    clean_primary = raw_primary.strip() if raw_primary else ""
    if clean_primary.startswith("models/"):
        clean_primary = clean_primary.removeprefix("models/").strip()
    primary = clean_primary or default_primary

    # 3.8 is EXCLUSIVELY for deep reasoning; remove from general chain
    chain = [primary] + [m for m in default_chain if m != primary and m != "gemini-3.8-flash"]
    deduped = list(dict.fromkeys(chain))

    # Strict token-aware filter: if on free tier and payload > 10k tokens, never include Gemma
    if (
        active_tier == "free"
        and estimated_tokens is not None
        and estimated_tokens > GEMMA_FREE_TIER_MAX_INPUT_TOKENS
    ):
        deduped = [m for m in deduped if "gemma" not in m.lower()]

    # On paid tier with heavy payloads, guarantee 4M TPM models lead the chain
    if (
        active_tier == "paid"
        and estimated_tokens is not None
        and estimated_tokens > PAYLOAD_HEAVY_THRESHOLD_TOKENS
    ):
        # These are the specific models known to have 4M TPM on Paid Tier
        high_tpm_models = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite"}
        high_tpm_leaders = [m for m in deduped if m in high_tpm_models]
        rest = [m for m in deduped if m not in high_tpm_leaders]
        deduped = high_tpm_leaders + rest

    available = _DEPLETED_MODEL_REGISTRY.filter_chain(deduped)
    final_chain = available if available else deduped
    logger.debug(
        "Resolved %s model chain (tokens=%s): %s",
        active_tier,
        estimated_tokens,
        final_chain,
    )
    return final_chain


def _select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type, active tier, and content commands.

    On Free Tier, defaults primary to gemini-3.5-flash-lite (500 RPD) to protect gemini-3.8-flash (20 RPD).
    On Paid Tier, defaults primary to gemini-3.5-flash-lite (150,000 RPD) for high-volume stability.
    Routes heavy workloads (pull_request.opened, slash commands, @mentions)
    to the primary model, and routine lifecycle events to the lightweight model.
    """
    active_tier = _resolve_tier()
    default_primary = "gemini-3.5-flash-lite"
    default_lightweight = "gemini-3.5-flash-lite"
    primary = os.environ.get(
        "PRIMARY_MODEL",
        os.environ.get("GEMMA_MODEL", default_primary),
    )
    lightweight = os.environ.get(
        "LIGHTWEIGHT_MODEL",
        os.environ.get("GEMMA_LIGHTWEIGHT_MODEL", default_lightweight),
    )

    # Fixed: Use the dynamic constant from constants.py, but the test expects
    # the env var override to still work. Since ENABLE_DYNAMIC_MODEL_ROUTING is now a
    # constant "1", the 'if' block is skipped.
    # To fix the test, we must ensure the code still honors the env var if present,
    # OR we change the test to use the constant.
    # Let's make it honor env var first, fallback to constant.
    from webhook_agent.constants import ENABLE_DYNAMIC_MODEL_ROUTING as DEFAULT_ROUTING

    routing_enabled = os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", DEFAULT_ROUTING)
    if routing_enabled not in ("1", "true", "True"):
        target = primary
    else:
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})

        pr_diff_text = str(raw.get("pr_diff") or "")
        est_diff_tokens = len(pr_diff_text) // 4 if pr_diff_text else 0

        if canonical in ("pull_request.opened", "pull_request.synchronize"):
            if active_tier == "paid" and est_diff_tokens > PAYLOAD_HEAVY_THRESHOLD_TOKENS:
                target = "gemini-3.8-flash"
            else:
                target = primary
        elif canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
            comment_body = ""
            if isinstance(raw.get("comment"), dict):
                comment_body = raw["comment"].get("body") or ""

            from webhook_agent.core.commands import has_review_slash_command

            cb_lower = comment_body.lower()
            if any(kw in cb_lower for kw in DEEP_REASONING_KEYWORDS):
                deep_primary = "gemini-2.5-pro" if active_tier == "paid" else "gemini-3.8-flash"
                target = os.environ.get("DEEP_REASONING_MODEL", deep_primary)
            elif has_review_slash_command(comment_body) or "@hannibal-hub-agents" in cb_lower:
                target = primary
            else:
                target = lightweight
        else:
            target = lightweight

    if _DEPLETED_MODEL_REGISTRY.is_depleted(target):
        chain = get_model_chain()
        target = chain[0] if chain else target

    return target


def get_active_model(event_data: dict[str, Any] | None = None) -> str:
    """Return default active model name for the agent, using dynamic model routing and depletion registry."""
    if event_data is not None:
        return _select_model_for_event(event_data)
    chain = get_model_chain()
    if chain:
        return chain[0]
    default_primary = "gemini-3.5-flash-lite"
    return os.environ.get(
        "PRIMARY_MODEL",
        os.environ.get("GEMMA_MODEL", default_primary),
    )


def _get_model_tpm_limit(model: str = "default", tier: str | None = None) -> int:
    """Reads TPM limit for model and tier from gemini_models.json."""
    active_tier = tier or _resolve_tier()
    target_model = model if model and model != "default" else get_active_model()
    try:
        import json
        from pathlib import Path

        candidates = [
            Path(__file__).resolve().parents[1] / "assets" / "registries" / "gemini_models.json",
            Path(__file__).resolve().parents[2] / "assets" / "registries" / "gemini_models.json",
            Path(__file__).resolve().parents[3] / "assets" / "registries" / "gemini_models.json",
        ]
        registry_path = next((p for p in candidates if p.exists()), candidates[0])
        if registry_path.exists():
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            full_key = (
                target_model if target_model.startswith("models/") else f"models/{target_model}"
            )
            for m in data.get("models", []):
                if isinstance(m, dict) and m.get("name") in (target_model, full_key):
                    rate_limits = m.get("rate_limits", {})
                    tier_data = rate_limits.get(active_tier, {})
                    if isinstance(tier_data, dict):
                        tpm_val = tier_data.get("tpm", 0)
                        if isinstance(tpm_val, (int, float)) and tpm_val > 0:
                            return int(tpm_val)
    except Exception as err:
        logger.debug("Failed reading registry TPM limits, using fallback: %s", err)
    return 15000 if active_tier == "free" else 100000


def _count_tokens_exact(text: str, model: str | None = None) -> int:
    """Uses Google GenAI free count_tokens API method with proper active key, model, and tier."""
    if not text:
        return 0
    active_key = get_active_api_key()
    if not active_key:
        return len(text) // 4

    target_model = model if model and model != "default" else get_active_model()
    try:
        from google import genai

        client = genai.Client(api_key=active_key)
        resp = client.models.count_tokens(model=target_model, contents=text)
        if resp and resp.total_tokens:
            return int(resp.total_tokens)
    except Exception as err:
        logger.debug("count_tokens API call failed, using heuristic: %s", err)
    return max(1, len(text) // 4)


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried.

    Transient errors include server unavailability (503), rate limiting (429),
    RESOURCE_EXHAUSTED errors, TPM/quota limit fast-fails, and other temporary issues.
    """
    if isinstance(error, GenAIServerError):
        error_code = getattr(error, "code", None)
        return error_code in (503, 500, 429, 502, 504)
    err_str = str(error).lower()
    err_type = type(error).__name__.lower()
    return (
        "429" in err_str
        or "503" in err_str
        or "unavailable" in err_str
        or "high demand" in err_str
        or "resource_exhausted" in err_str
        or "resourceexhausted" in err_type
        or "tpm limit" in err_str
        or "quota" in err_str
        or "rate limit" in err_str
        or "clienterror" in err_type
        or "servererror" in err_type
        or "ratelimitexceedederror" in err_type
    )


def is_gemini_3_plus(model_name: str | None) -> bool:
    """Check if a model is Gemini 3+ and supports explicit context caching.

    Gemma models (e.g. gemma-4-31b-it) and pre-Gemini-3 models do not support
    context caching. Context caching must only be enabled for Gemini 3+ models.
    """
    if not model_name:
        return False
    clean = model_name.replace("models/", "").strip().lower()
    if "gemma" in clean:
        return False
    import re

    match = re.search(r"gemini-(\d+(?:\.\d+)?)", clean)
    if match:
        try:
            version = float(match.group(1))
            return version >= 3.0
        except ValueError:
            return False
    return False
