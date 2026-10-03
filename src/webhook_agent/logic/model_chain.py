"""Model chain routing, capacity ordering, dynamic event selection, and transient error detection.

Extracted from webhook_agent.py as Phase 1 of codebase modularization.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from google.genai.errors import ServerError as GenAIServerError

from webhook_agent.logic.rate_limiter import (
    _resolve_tier,
    get_active_api_key,
)

logger = logging.getLogger("webhook_agent.model_chain")

# ---------------------------------------------------------------------------
# Process-Wide Model Depletion Registry
# ---------------------------------------------------------------------------


class DepletedModelRegistry:
    """Tracks models that have hit 429 quota exhaustion to bypass them process-wide across events."""

    def __init__(self, default_cooldown: float = 3600.0) -> None:
        self.default_cooldown = default_cooldown
        self._depleted: dict[str, tuple[float, float]] = {}

    def _norm(self, name: str) -> str:
        return name.replace("models/", "").strip().lower()

    def mark_depleted(self, model_name: str, error: Exception | None = None) -> None:
        norm_name = self._norm(model_name)
        cooldown = self.default_cooldown
        metric_type = "DEFAULT (1h)"

        if error is not None:
            err_str = str(error).lower()
            if "perday" in err_str or "dayperproject" in err_str:
                cooldown = 86400.0
                metric_type = "RPD (24h)"
            elif (
                "perminute" in err_str
                or "minuteperproject" in err_str
                or "tokensperminute" in err_str
                or "429" in err_str
            ):
                cooldown = 60.0
                metric_type = "RPM/TPM (60s)"
            elif "503" in err_str or "unavailable" in err_str or "high demand" in err_str:
                cooldown = 120.0
                metric_type = "503 HIGH DEMAND (120s)"

        self._depleted[norm_name] = (time.time(), cooldown)
        logger.warning(
            "Model '%s' marked DEPLETED [%s] across process",
            norm_name,
            metric_type,
        )

    def is_depleted(self, model_name: str) -> bool:
        norm_name = self._norm(model_name)
        if norm_name not in self._depleted:
            return False
        timestamp, cooldown = self._depleted[norm_name]
        if time.time() - timestamp > cooldown:
            del self._depleted[norm_name]
            logger.info(
                "Model '%s' depletion cooldown expired (%ds), restored to pool",
                norm_name,
                int(cooldown),
            )
            return False
        return True

    def filter_chain(self, chain: list[str]) -> list[str]:
        return [m for m in chain if not self.is_depleted(m)]


_DEPLETED_MODEL_REGISTRY: Any
try:
    from webhook_agent.logic.firestore_registry import (
        firestore_depleted_registry as _DEPLETED_MODEL_REGISTRY,
    )
except ImportError:
    _DEPLETED_MODEL_REGISTRY = DepletedModelRegistry(default_cooldown=3600.0)


# ---------------------------------------------------------------------------
# Chain Resolution & Event Selection
# ---------------------------------------------------------------------------


def get_model_chain() -> list[str]:
    """Build ordered list of fallback models sorted by capacity and tier.

    Filters out models currently marked as depleted in _DEPLETED_MODEL_REGISTRY.
    See docs/MODEL_CHAIN.md for the canonical reference.

    Free Tier Chain:
        1. gemini-3.5-flash-lite (500 RPD / 250k TPM)
        2. gemini-3.1-flash-lite (500 RPD / 250k TPM)
        3. gemini-2.5-flash (20 RPD / 250k TPM)
        4. gemini-2.5-flash-lite (20 RPD / 250k TPM)
        5. gemma-4-31b-it (14,400 RPD / 16k TPM)
        6. gemma-4-26b-a4b-it (14,400 RPD / 16k TPM)

    Paid Tier Chain:
        1. gemini-3.8-flash (10,000 RPD / 2M TPM)
        2. gemini-3.7-flash (10,000 RPD / 2M TPM)
        3. gemini-3.6-flash (10,000 RPD / 2M TPM)
        4. gemini-3.5-flash-lite (150,000 RPD / 4M TPM)
        5. gemini-3.1-flash-lite (150,000 RPD / 4M TPM)
        6. gemini-2.5-flash (10,000 RPD / 1M TPM)
        7. gemini-2.5-flash-lite (1,000,000 RPD / 4M TPM)
    """
    active_tier = _resolve_tier()
    if active_tier == "paid":
        default_primary = "gemini-3.8-flash"
        default_chain = [
            default_primary,
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
    else:
        default_primary = "gemini-3.5-flash-lite"
        default_chain = [
            default_primary,
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        ]

    # Support PRIMARY_MODEL with fallback to legacy GEMMA_MODEL
    primary = os.environ.get(
        "PRIMARY_MODEL",
        os.environ.get("GEMMA_MODEL", default_primary),
    )
    chain = [primary] + [m for m in default_chain if m != primary]
    deduped = list(dict.fromkeys(chain))
    available = _DEPLETED_MODEL_REGISTRY.filter_chain(deduped)
    final_chain = available if available else deduped
    logger.debug("Resolved %s model chain: %s", active_tier, final_chain)
    return final_chain


def _select_model_for_event(event_data: dict[str, Any]) -> str:
    """Select appropriate model based on event type, active tier, and content commands.

    On Free Tier, defaults primary to gemini-3.5-flash-lite (500 RPD) to protect gemini-3.8-flash (20 RPD).
    Routes heavy workloads (pull_request.opened, slash commands, @mentions)
    to the primary model, and routine lifecycle events to the lightweight model.
    """
    active_tier = _resolve_tier()
    default_primary = "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.8-flash"
    primary = os.environ.get(
        "PRIMARY_MODEL",
        os.environ.get("GEMMA_MODEL", default_primary),
    )
    lightweight = os.environ.get(
        "LIGHTWEIGHT_MODEL",
        os.environ.get("GEMMA_LIGHTWEIGHT_MODEL", "gemini-3.5-flash-lite"),
    )

    if os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", "1") not in (
        "1",
        "true",
        "True",
    ):
        target = primary
    else:
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})

        if canonical in ("pull_request.opened", "pull_request.synchronize"):
            target = primary
        elif canonical.startswith(("issue_comment.", "pull_request_review_comment.")):
            comment_body = ""
            if isinstance(raw.get("comment"), dict):
                comment_body = raw["comment"].get("body") or ""

            commands = (
                "/review",
                "/create",
                "/resolve",
                "/help",
                "@hannibal-hub-agents",
            )
            if any(cmd in comment_body for cmd in commands):
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
    active_tier = _resolve_tier()
    default_primary = "gemini-3.5-flash-lite" if active_tier == "free" else "gemini-3.8-flash"
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
            Path(__file__).resolve().parents[2] / "assets" / "registries" / "gemini_models.json",
            Path(__file__).resolve().parents[3] / "assets" / "registries" / "gemini_models.json",
        ]
        registry_path = next((p for p in candidates if p.exists()), candidates[1])
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
    except Exception:
        pass
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
    except Exception:
        pass
    return max(1, len(text) // 4)


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried.

    Transient errors include server unavailability (503), rate limiting (429),
    RESOURCE_EXHAUSTED errors, and other temporary issues.
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
        or "clienterror" in err_type
        or "servererror" in err_type
    )
