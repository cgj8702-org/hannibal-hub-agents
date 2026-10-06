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
    calculate_verdict,
    count_tokens_exact,
    get_max_input_tokens,
)
from webhook_agent.core.github_tools import (
    add_comment,
    add_label,
    get_commit_diff,
    get_current_time,
    get_issue,
    mark_ready_for_review,
    merge_pr,
    open_pr,
    read_file,
    review,
    update_branch_from_base,
    update_issue,
    write_file,
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
    "add_comment",
    "add_label",
    "calculate_verdict",
    "count_tokens_exact",
    "get_commit_diff",
    "get_current_time",
    "get_issue",
    "get_max_input_tokens",
    "get_shared_genai_client",
    "get_shared_text_generation_provider",
    "mark_ready_for_review",
    "merge_pr",
    "open_pr",
    "read_file",
    "review",
    "run_in_bg_loop",
    "update_branch_from_base",
    "update_issue",
    "write_file",
]
