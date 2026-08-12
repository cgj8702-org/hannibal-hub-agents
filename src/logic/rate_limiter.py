"""
Rate Limiter for Hannibal Hub Agents API calls.

Ported from hannibal-hub with a deliberate tier-resolution adaptation:
- hannibal-hub resolves tier via CHAT_KEY + HANNIBAL_TIER.
- hannibal-hub-agents resolves tier via FREE_KEY / PAID_KEY / GEMINI_API_KEY.

Supports:
- Dual-tier (free/paid) per-model RPM/TPM/RPD limits loaded from the registry.
- Sliding-window TPM token expiration (multi-request aware).
- Burst RPM handling up to the model's per-minute limit.
- Zero-quota fast-fail: models with 0 RPM/RPD on the active tier are rejected.
"""

import asyncio
import collections
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("hannibal_rate_limiter")


def _load_rate_limits(registry_path: Path) -> dict[str, dict[str, Any]]:
    """Dynamically load rate limits (rpm and tpm) for free and paid tiers from registry JSON."""
    try:
        if registry_path.exists():
            return json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to load rate_limits.json: %s", e)
    return {}


def get_active_api_key() -> str:
    """Get active API key from WEBHOOK_PAID_KEY / PAID_KEY or WEBHOOK_FREE_KEY / FREE_KEY based on resolved tier and synchronize os.environ."""
    tier = _resolve_tier()
    if tier == "paid":
        key = (
            os.getenv("WEBHOOK_PAID_KEY")
            or os.getenv("PAID_KEY")
            or os.getenv("WEBHOOK_FREE_KEY")
            or os.getenv("FREE_KEY")
            or os.getenv("GEMINI_API_KEY", "")
        )
    else:
        key = (
            os.getenv("WEBHOOK_FREE_KEY")
            or os.getenv("FREE_KEY")
            or os.getenv("WEBHOOK_PAID_KEY")
            or os.getenv("PAID_KEY")
            or os.getenv("GEMINI_API_KEY", "")
        )

    if key:
        os.environ["GEMINI_API_KEY"] = key
        os.environ["GOOGLE_API_KEY"] = key

    return key


def _resolve_tier() -> str:
    """Resolve the active tier using the hannibal-hub-agents Key Resolution Protocol.

    Resolution cascade:
    1. Explicit override: HANNIBAL_TIER env var ("free" or "paid").
    2. Active key match: If GEMINI_API_KEY matches WEBHOOK_FREE_KEY/FREE_KEY or WEBHOOK_PAID_KEY/PAID_KEY.
    3. Presence fallback: WEBHOOK_PAID_KEY or PAID_KEY exists and is non-empty/non-dummy -> paid, else free.
    """
    explicit_tier = os.getenv("HANNIBAL_TIER", "").lower()
    if explicit_tier in ("free", "paid"):
        return explicit_tier

    free_keys = {k for k in (os.getenv("WEBHOOK_FREE_KEY"), os.getenv("FREE_KEY")) if k}
    paid_keys = {k for k in (os.getenv("WEBHOOK_PAID_KEY"), os.getenv("PAID_KEY")) if k}
    active_gemini_key = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))

    if (
        active_gemini_key
        and active_gemini_key in free_keys
        and active_gemini_key not in paid_keys
    ):
        return "free"
    if active_gemini_key and active_gemini_key in paid_keys:
        return "paid"

    for p_key in paid_keys:
        if p_key.lower() not in ("", "dummy", "none"):
            return "paid"

    return "free"


class RPMWaiter:
    """Sliding-window rate limiter keyed by model with free/paid tier awareness."""

    def __init__(
        self,
        registry_path: Path | None = None,
        default_limit: int = 10,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.default_limit = default_limit
        self.window = window
        self.histories: dict[str, list[float]] = collections.defaultdict(list)
        self.token_histories: dict[str, list[Any]] = collections.defaultdict(list)
        self.lock = asyncio.Lock()
        self.clock = clock
        # src/logic/rate_limiter.py -> src/assets/registries/rate_limits.json
        self.registry_path = registry_path or (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "registries"
            / "rate_limits.json"
        )
        self.model_limits = _load_rate_limits(self.registry_path)

    async def check_and_wait(
        self,
        model: str = "default",
        rpm_override: int | None = None,
        estimated_tokens: int = 0,
        tier: str | None = None,
    ) -> None:
        """Check RPM/TPM limits for the given model, sleeping to respect them.

        Args:
            model: Model name (with or without the 'models/' prefix).
            rpm_override: Optional explicit RPM limit (bypasses registry).
            estimated_tokens: Estimated input+output tokens for TPM accounting.
            tier: Active tier ("free" or "paid"). Resolved from env if omitted.

        Raises:
            ValueError: If the model has 0 RPM/RPD quota on the active tier.
        """
        wait_time = 0.0

        if not tier:
            tier = _resolve_tier()

        full_model_key = model if model.startswith("models/") else f"models/{model}"
        model_entry = self.model_limits.get(
            model, self.model_limits.get(full_model_key, {})
        )
        if isinstance(model_entry, dict) and tier in model_entry:
            tier_entry = model_entry[tier]
        else:
            tier_entry = model_entry if isinstance(model_entry, dict) else {}

        rpm_limit = (
            rpm_override
            if rpm_override is not None
            else tier_entry.get("rpm", self.default_limit)
        )
        # Zero-quota fast-fail: reject models with 0 RPM/RPD on the active tier
        # (e.g. gemini-2.0-flash / gemini-2.0-flash-lite on Free Tier) immediately.
        if (
            rpm_override is None
            and tier_entry
            and (tier_entry.get("rpm") == 0 or tier_entry.get("rpd") == 0.0)
        ):
            logger.warning(
                "FAST FAIL (%s): Model has 0 quota on tier '%s'. Rejecting.",
                model,
                tier,
            )
            raise ValueError(
                f"Model '{model}' is unavailable on tier '{tier}' (0 quota)."
            )

        if rpm_limit <= 0:
            rpm_limit = self.default_limit

        tpm_limit = tier_entry.get("tpm", 0)

        async with self.lock:
            now = self.clock()
            history = self.histories[model]
            token_history = self.token_histories[model]

            # Prune old RPM & TPM histories
            history[:] = [t for t in history if now - t <= self.window]
            token_history[:] = [
                entry for entry in token_history if now - entry[0] <= self.window
            ]

            # 1. RPM Check (bursts allowed up to limit)
            wait_rpm = 0.0
            if len(history) >= rpm_limit:
                oldest_ts = history[0]
                wait_rpm = max(0.1, (oldest_ts + self.window) - now)
                logger.warning(
                    "RPM THROTTLE (%s): Used %d/%d. Sleeping %.1fs...",
                    model,
                    len(history),
                    rpm_limit,
                    wait_rpm,
                )

            # 2. TPM Check (exact sliding window token expiration)
            wait_tpm = 0.0
            if tpm_limit > 0 and estimated_tokens > 0:
                active_tpm = sum(tok for _, tok, _ in token_history)
                if active_tpm + estimated_tokens > tpm_limit:
                    needed_tokens_to_expire = (
                        active_tpm + estimated_tokens
                    ) - tpm_limit
                    accumulated = 0
                    required_ts = now
                    for entry in token_history:
                        ts, tok = entry[0], entry[1]
                        accumulated += tok
                        required_ts = ts
                        if accumulated >= needed_tokens_to_expire:
                            break
                    wait_tpm = max(0.1, (required_ts + self.window) - now)
                    logger.warning(
                        "TPM THROTTLE (%s): Active %d+%d/%d TPM limit exceeded. "
                        "Waiting %.1fs for tokens to expire...",
                        model,
                        active_tpm,
                        estimated_tokens,
                        tpm_limit,
                        wait_tpm,
                    )

            wait_time = max(wait_rpm, wait_tpm)

            # Reserve slot
            history.append(now + wait_time)
            if estimated_tokens > 0:
                token_history.append([now + wait_time, estimated_tokens, False])

        if wait_time > 0:
            await asyncio.sleep(wait_time)

    async def record_actual_tokens(
        self, model: str = "default", actual_tokens: int = 0
    ) -> None:
        """Update or record real token usage returned in the provider API response."""
        if actual_tokens <= 0:
            return

        async with self.lock:
            now = self.clock()
            token_history = self.token_histories[model]

            token_history[:] = [
                entry for entry in token_history if now - entry[0] <= self.window
            ]

            # Update the earliest estimated (unfinalized) token reservation
            unfinalized = next((entry for entry in token_history if not entry[2]), None)
            if unfinalized:
                unfinalized[1] = actual_tokens
                unfinalized[2] = True
            else:
                token_history.append([now, actual_tokens, True])


rpm_waiter = RPMWaiter()
