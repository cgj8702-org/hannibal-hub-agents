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
from webhook_agent.core.callbacks import (
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from webhook_agent.core.cancellation import (
    AbortAgentExecution,
    PRClosedRegistry,
    pr_closed_registry,
)
from webhook_agent.core.loop_helpers import (
    _ensure_bg_loop,
    get_shared_genai_client,
    get_shared_text_generation_provider,
    run_in_bg_loop,
)
from webhook_agent.core.memory_service import InMemoryMemoryService
from webhook_agent.core.sanitizer_plugin import PromptSanitizerPlugin
from webhook_agent.tools.github_tools import (
    add_label,
    create_issue,
    get_commit_diff,
    get_current_time,
    get_issue,
    read_file,
    review,
)

__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "BOT_LOGIN",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "SYSTEM_INSTRUCTION",
    "AbortAgentExecution",
    "InMemoryMemoryService",
    "PRClosedRegistry",
    "PromptSanitizerPlugin",
    "WebhookAgent",
    "_ensure_bg_loop",
    "add_label",
    "after_model_callback",
    "after_tool_callback",
    "before_agent_callback",
    "before_model_callback",
    "before_tool_callback",
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
    "on_tool_error_callback",
    "pr_closed_registry",
    "read_file",
    "review",
    "run_in_bg_loop",
]
