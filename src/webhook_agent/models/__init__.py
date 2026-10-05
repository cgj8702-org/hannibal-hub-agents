"""Model orchestration, fallback ladders, quota rate limiters, and provider factories."""

from __future__ import annotations

from .firestore_registry import FirestoreDepletedModelRegistry, firestore_depleted_registry
from .genai_provider import (
    GenerateContentProvider,
    TextGenerationProvider,
    TextGenerationResult,
    get_text_generation_provider,
)
from .model_chain import (
    DepletedModelRegistry,
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
from .review_budget import compute_round_allowance, summarise_review_history
from .secret_manager import resolve_secret

__all__ = [
    "DepletedModelRegistry",
    "FirestoreDepletedModelRegistry",
    "GenerateContentProvider",
    "RPMWaiter",
    "RateLimitExceededError",
    "RateLimitedGemini",
    "TextGenerationProvider",
    "TextGenerationResult",
    "compute_round_allowance",
    "extract_rate_limit_details",
    "firestore_depleted_registry",
    "get_active_api_key",
    "get_active_model",
    "get_adk_model",
    "get_allowed_models",
    "get_model_chain",
    "get_text_generation_provider",
    "is_gemini_3_plus",
    "resolve_secret",
    "resolve_webhook_api_key",
    "rpm_waiter",
    "summarise_review_history",
]
