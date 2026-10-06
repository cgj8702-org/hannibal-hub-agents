"""Core package for webhook_agent.

Provides the primary WebhookAgent implementation, event loop helpers,
and ADK GitHub tools.
"""

from __future__ import annotations

from webhook_agent.core.agent_definition import (
    AUDITOR_CONTEXT_INSTRUCTION,
    BOT_LOGIN,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    SYSTEM_INSTRUCTION,
    WebhookAgent,
    build_user_message,
    calculate_verdict,
    count_tokens_exact,
    execute_agent_event,
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

__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "BOT_LOGIN",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "SYSTEM_INSTRUCTION",
    "WebhookAgent",
    "_ensure_bg_loop",
    "add_label",
    "build_user_message",
    "calculate_verdict",
    "count_tokens_exact",
    "create_issue",
    "execute_agent_event",
    "get_commit_diff",
    "get_current_time",
    "get_issue",
    "get_max_input_tokens",
    "get_shared_genai_client",
    "get_shared_text_generation_provider",
    "read_file",
    "review",
    "run_in_bg_loop",
]
