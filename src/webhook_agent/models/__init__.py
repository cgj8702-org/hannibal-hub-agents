"""Model orchestration, fallback ladders, quota rate limiters, and provider factories."""

from __future__ import annotations

from .depleted_registry import (
    DepletedModelRegistry,
    FirestoreDepletedModelRegistry,
    firestore_depleted_registry,
)
from .model_chain import (
    get_active_model,
    get_model_chain,
    is_gemini_3_plus,
)
from .model_factory import RateLimitedGemini, get_adk_model
from .rate_limiter import (
    RateLimitExceededError,
    RPMWaiter,
    extract_rate_limit_details,
    get_active_api_key,
    get_allowed_models,
    resolve_webhook_api_key,
    rpm_waiter,
)
from .secret_manager import resolve_secret

__all__ = [
    "DepletedModelRegistry",
    "FirestoreDepletedModelRegistry",
    "RPMWaiter",
    "RateLimitExceededError",
    "RateLimitedGemini",
    "extract_rate_limit_details",
    "firestore_depleted_registry",
    "get_active_api_key",
    "get_active_model",
    "get_adk_model",
    "get_allowed_models",
    "get_model_chain",
    "is_gemini_3_plus",
    "resolve_secret",
    "resolve_webhook_api_key",
    "rpm_waiter",
]
