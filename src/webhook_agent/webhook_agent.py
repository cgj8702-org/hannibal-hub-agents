"""ADK-powered webhook agent for GitHub PR code review.

This module re-exports the modularized core WebhookAgent and related
primitives, maintaining 100% backwards compatibility with existing consumers
and test suites.
"""

from __future__ import annotations

import logging

from google.adk.runners import Runner

from webhook_agent.core.agent_definition import (
    _FALLBACK_MODEL,
    _MAX_RETRIES,
    AUDITOR_CONTEXT_INSTRUCTION,
    BOT_LOGIN,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    SYSTEM_INSTRUCTION,
    WebhookAgent,
    _truncate_input_for_tier,
    _truncate_text_to_token_limit,
    calculate_verdict,
    count_tokens_exact,
    get_max_input_tokens,
)
from webhook_agent.core.github_tools import (
    add_label,
    create_issue,
    get_commit_diff,
    get_current_time,
    get_issue,
    read_file,
    review,
)
from webhook_agent.core.loop_helpers import (
    _ensure_bg_loop,
    get_shared_genai_client,
    get_shared_text_generation_provider,
    run_in_bg_loop,
)
from webhook_agent.core.plugins import (
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)
from webhook_agent.models.model_chain import (
    _DEPLETED_MODEL_REGISTRY,
    DepletedModelRegistry,
    _count_tokens_exact,
    _get_model_tpm_limit,
    _is_transient_error,
    _select_model_for_event,
    get_active_model,
    get_model_chain,
)
from webhook_agent.models.model_factory import RateLimitedGemini, get_adk_model
from webhook_agent.models.rate_limiter import (
    extract_rate_limit_details,
    get_active_api_key,
    rpm_waiter,
)
from webhook_agent.review.review_enforcer import (
    _enforce_verdict,
    _parse_scorecard_scores,
    _submit_formal_review,
)
from webhook_agent.review.writeback_policy import (
    _COMMENT_RATE_LIMITER,
    CommentRateLimiter,
    _is_formal_review_eligible,
    _review_lock,
    evaluate_writeback_policy,
)

logger = logging.getLogger("webhook_agent.agent")

__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "BOT_LOGIN",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "SYSTEM_INSTRUCTION",
    "_COMMENT_RATE_LIMITER",
    "_DEPLETED_MODEL_REGISTRY",
    "_FALLBACK_MODEL",
    "_MAX_RETRIES",
    "CommentRateLimiter",
    "DepletedModelRegistry",
    "RateLimitedGemini",
    "Runner",
    "ToolOutputPruningPlugin",
    "WebhookAgent",
    "WebhookHistoryPruningPlugin",
    "_count_tokens_exact",
    "_enforce_verdict",
    "_ensure_bg_loop",
    "_get_model_tpm_limit",
    "_is_formal_review_eligible",
    "_is_transient_error",
    "_parse_scorecard_scores",
    "_review_lock",
    "_select_model_for_event",
    "_submit_formal_review",
    "_truncate_input_for_tier",
    "_truncate_text_to_token_limit",
    "add_label",
    "calculate_verdict",
    "count_tokens_exact",
    "create_issue",
    "evaluate_writeback_policy",
    "extract_rate_limit_details",
    "get_active_api_key",
    "get_active_model",
    "get_adk_model",
    "get_commit_diff",
    "get_current_time",
    "get_issue",
    "get_max_input_tokens",
    "get_model_chain",
    "get_shared_genai_client",
    "get_shared_text_generation_provider",
    "read_file",
    "review",
    "rpm_waiter",
    "run_in_bg_loop",
]
