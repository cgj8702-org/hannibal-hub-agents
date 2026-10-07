"""WebhookAgent ADK agent definition and execution orchestration.

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from github import Github
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Workflow
from google.genai import types as genai_types

from webhook_agent.core.callbacks import (
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from webhook_agent.core.execution import execute_agent_event
from webhook_agent.core.loop_helpers import (
    get_shared_genai_client,
)
from webhook_agent.core.memory_service import InMemoryMemoryService
from webhook_agent.core.plugins import (
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)
from webhook_agent.core.prompts import (
    AUDITOR_CONTEXT_INSTRUCTION,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    SYSTEM_INSTRUCTION,
    _truncate_input_for_tier,
    _truncate_text_to_token_limit,
    build_user_message,
    get_max_input_tokens,
)
from webhook_agent.core.sanitizer_plugin import PromptSanitizerPlugin
from webhook_agent.github.bot_identity import _is_bot_event
from webhook_agent.models.model_chain import (
    _DEPLETED_MODEL_REGISTRY,
    get_active_model,
    get_model_chain,
    is_gemini_3_plus,
)
from webhook_agent.models.model_factory import get_adk_model
from webhook_agent.models.rate_limiter import (
    get_active_api_key,
)
from webhook_agent.review.verdict_parser import calculate_verdict
from webhook_agent.review.writeback_policy import (
    evaluate_writeback_policy,
)
from webhook_agent.tools.github_tools import (
    add_label,
    create_issue,
    get_commit_diff,
    get_current_time,
    get_issue,
    read_file,
    review,
)
from webhook_agent.tools.search_tool import google_search_grounding_tool
from webhook_agent.webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.core.agent_definition")

# Retry configuration for transient server errors
_MAX_RETRIES = int(
    os.environ.get("PRIMARY_MODEL_MAX_RETRIES") or os.environ.get("GEMMA_MODEL_MAX_RETRIES") or "5"
)
_FALLBACK_MODEL = (
    os.environ.get("PRIMARY_MODEL_FALLBACK")
    or os.environ.get("GEMMA_MODEL_FALLBACK")
    or "gemini-3.5-flash-lite"
)

# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "BOT_LOGIN",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "SYSTEM_INSTRUCTION",
    "_FALLBACK_MODEL",
    "_MAX_RETRIES",
    "WebhookAgent",
    "_truncate_input_for_tier",
    "_truncate_text_to_token_limit",
    "build_user_message",
    "calculate_verdict",
    "count_tokens_exact",
    "execute_agent_event",
    "get_max_input_tokens",
]


def count_tokens_exact(contents: str | list[Any], model_name: str | None = None) -> int | None:
    """Count input tokens using Google GenAI SDK's client.models.count_tokens()."""
    target_model = model_name or get_active_model()
    try:
        client = get_shared_genai_client()
        if client is None:
            return None
        res = client.models.count_tokens(
            model=target_model,
            contents=contents if isinstance(contents, list) else [contents],
        )
        return getattr(res, "total_tokens", None)
    except Exception as exc:
        logger.debug("count_tokens API call skipped/unavailable: %s", exc)
        return None


class WebhookAgent:
    """ADK-powered agent for processing GitHub webhook events.

    Wraps the ADK Agent and Runner to provide a synchronous interface
    compatible with the existing webhook pipeline.

    Supports automatic model fallback when the primary model is unavailable.
    """

    def __init__(
        self,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._app_name = "hannibal-hub-agents"

        # Session service — keeps per-PR conversation history
        self._session_service = InMemorySessionService()

        # Memory service — in-memory conversation memory
        self._memory_service = InMemoryMemoryService()

        # Track current model chain (TPM Descending)
        self._model_chain = get_model_chain()
        self._chain_index = 0
        self._current_model_name = self._model_chain[self._chain_index]
        self._attempted_model_names = {self._normalize_model_name(self._current_model_name)}
        self._fallback_triggered = False

        # Ensure API key is resolved and propagated to env vars before model init
        get_active_api_key()

        # Model instance for pipeline sub-agents
        model_instance = get_adk_model(
            model_name=self._current_model_name,
            api_key=get_active_api_key(),
        )
        PromptSanitizerPlugin()

        # Streamlined workflow: START -> code_auditor
        # Single-pass high-velocity auditor eliminating redundant sub-agent hops.
        auditor_thinking_budget = int(os.environ.get("AUDITOR_THINKING_BUDGET", "1024"))

        self._code_auditor = LlmAgent(
            name="code_auditor",
            model=model_instance,
            include_contents="default",
            description="Conducts AST diff-grounded risk audit using Gemini Thinking Mode.",
            instruction=AUDITOR_CONTEXT_INSTRUCTION + SYSTEM_INSTRUCTION,
            output_key="code_review_analysis",
            planner=BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=False,
                    thinking_budget=auditor_thinking_budget,
                )
            ),
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                get_issue,
                get_commit_diff,
                get_current_time,
                google_search_grounding_tool,
                review,
            ],
        )

        self._agent = Workflow(
            name="webhook_agent",
            edges=[
                (START, self._code_auditor),
            ],
        )

        self._conversational_agent = LlmAgent(
            name="conversational_agent",
            model=model_instance,
            include_contents="default",
            description="Collaborative pair programming engineer for conversational discussions in GitHub threads.",
            instruction=CONVERSATIONAL_INSTRUCTION,
            output_key="conversational_reply",
            planner=BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=False,
                    thinking_budget=min(auditor_thinking_budget, 512),
                )
            ),
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                get_issue,
                get_commit_diff,
                get_current_time,
                google_search_grounding_tool,
                add_label,
                create_issue,
            ],
        )

        self._conversational_workflow = Workflow(
            name="conversational_workflow",
            edges=[
                (START, self._conversational_agent),
            ],
        )

        self._history_pruning_plugin = WebhookHistoryPruningPlugin(max_events=12)
        self._tool_pruning_plugin = ToolOutputPruningPlugin()

        # Context Caching: strictly only for Gemini 3+ models (min 4096 tokens).
        # Gemma models (e.g. gemma-4-31b-it) do NOT support context caching.
        context_cache_config = (
            ContextCacheConfig(
                min_tokens=4096,
                ttl_seconds=1800,
                cache_intervals=10,
            )
            if is_gemini_3_plus(self._current_model_name)
            else None
        )

        self._app = App(
            name=self._app_name,
            root_agent=self._agent,
            context_cache_config=context_cache_config,
            plugins=[
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
            ],
        )

        self._conversational_app = App(
            name=self._app_name,
            root_agent=self._conversational_workflow,
            context_cache_config=context_cache_config,
            plugins=[
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
            ],
        )

        # Create the runners
        self._runner = Runner(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

        self._conversational_runner = Runner(
            app=self._conversational_app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        return model_name.replace("models/", "").strip().lower()

    def _advance_model_chain(self, error: Exception | None = None) -> str | None:
        """Cascade once to the next untried model, or return None when exhausted."""
        failed_model = self._normalize_model_name(self._current_model_name)
        self._attempted_model_names.add(failed_model)
        _DEPLETED_MODEL_REGISTRY.mark_depleted(self._current_model_name, error=error)

        full_chain = get_model_chain()
        available = [
            model
            for model in full_chain
            if self._normalize_model_name(model) not in self._attempted_model_names
        ]
        self._model_chain = available
        self._chain_index = 0

        if not self._model_chain:
            logger.error("All configured models have been attempted for this agent run")
            return None

        next_model = self._model_chain[0]
        logger.warning(
            "⚠️ Cascading model chain from %s -> %s",
            self._current_model_name,
            next_model,
        )
        self._current_model_name = next_model
        self._attempted_model_names.add(self._normalize_model_name(next_model))
        new_model_instance = get_adk_model(
            model_name=next_model,
            api_key=get_active_api_key(),
        )
        if hasattr(self, "_code_auditor") and self._code_auditor is not None:
            self._code_auditor.model = new_model_instance
        if hasattr(self, "_conversational_agent") and self._conversational_agent is not None:
            self._conversational_agent.model = new_model_instance

        # Dynamically toggle context caching based on model family:
        # Strictly enable only for Gemini 3+ models; disable for Gemma / legacy models.
        if is_gemini_3_plus(next_model):
            cache_cfg = ContextCacheConfig(
                min_tokens=4096,
                ttl_seconds=1800,
                cache_intervals=10,
            )
            if hasattr(self, "_app") and self._app is not None:
                self._app.context_cache_config = cache_cfg
            if hasattr(self, "_conversational_app") and self._conversational_app is not None:
                self._conversational_app.context_cache_config = cache_cfg
        else:
            if hasattr(self, "_app") and self._app is not None:
                self._app.context_cache_config = None
            if hasattr(self, "_conversational_app") and self._conversational_app is not None:
                self._conversational_app.context_cache_config = None

        runner_cls = Runner
        self._runner = runner_cls(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )
        if hasattr(self, "_conversational_app") and self._conversational_app is not None:
            self._conversational_runner = runner_cls(
                app=self._conversational_app,
                session_service=self._session_service,
                memory_service=self._memory_service,
            )
        return next_model

    def _create_fallback_agent(self, error: Exception | None = None) -> None:
        """Switch to fallback model when primary model is unavailable."""
        self._advance_model_chain(error=error)

        # Recreate runner with new agent
        self._runner = Runner(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _derive_session_id(self, event_data: dict[str, Any]) -> str:
        """Derive a session ID from the event data for conversation continuity.

        Uses repo_full_name + issue/PR number so that follow-up comments
        on the same thread share a session.
        """
        repo = event_data.get("repository", {})
        repo_name = repo.get("full_name", "unknown")
        raw = event_data.get("raw_payload", {})
        issue = raw.get("issue", {})
        pr = raw.get("pull_request", {})
        number = issue.get("number") or pr.get("number")
        if number:
            return f"{repo_name}/{number}"
        return f"{repo_name}/{event_data.get('delivery_id', 'unknown')}"

    def _build_user_message(self, event_data: dict[str, Any]) -> genai_types.Content:
        """Build a user message from the webhook event data."""
        return build_user_message(event_data)

    def plan_and_execute(
        self,
        event_data: dict[str, Any],
        gh_client: Github,
        trace_id: str,
    ) -> list[ActionResult]:
        """Process a webhook event through the ADK agent.

        This is the main entry point, called from WebhookProcessor / AgentCore.run().

        Args:
            event_data: Normalized webhook event data.
            gh_client: Authenticated GitHub client.
            trace_id: Trace ID for logging.

        Returns:
            List of ActionResult objects.
        """
        # Check writeback policy (bot-authored, read-only, closed PR, mutations disabled, dry-run)
        policy_actions = evaluate_writeback_policy(
            event_data=event_data,
            dry_run=self.dry_run,
            trace_id=trace_id,
            is_bot_event_fn=_is_bot_event,
        )
        if policy_actions is not None:
            return policy_actions

        return execute_agent_event(
            agent=self,
            event_data=event_data,
            gh_client=gh_client,
            trace_id=trace_id,
        )
