"""WebhookAgent ADK agent definition and execution orchestration.

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import logging
from typing import Any

from github import Github
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
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
from webhook_agent.core.guardrails import GuardrailsPlugin, IterationBudgetPlugin
from webhook_agent.core.loop_helpers import (
    get_shared_genai_client,
)
from webhook_agent.core.plugins import (
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)
from webhook_agent.core.policy import build_thinking_config
from webhook_agent.core.prompts import (
    AUDITOR_CONTEXT_INSTRUCTION,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    STATIC_REVIEW_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    _truncate_input_for_tier,
    _truncate_text_to_token_limit,
    build_repo_profile_context,
    build_static_review_instruction,
    build_transient_turn_content,
    build_user_message,
    get_max_input_tokens,
)
from webhook_agent.core.sanitizer_plugin import PromptSanitizerPlugin
from webhook_agent.core.session_service import BoundedSessionService
from webhook_agent.github.bot_identity import _is_bot_event
from webhook_agent.models.model_chain import (
    _DEPLETED_MODEL_REGISTRY,
    get_active_model,
    get_model_chain,
    is_gemini_3_plus,
)
from webhook_agent.models.model_factory import get_adk_model
from webhook_agent.models.rate_limiter import (
    _resolve_tier,
    get_active_api_key,
)
from webhook_agent.review.writeback_policy import (
    evaluate_writeback_policy,
)
from webhook_agent.tools.github_tools import (
    create_issue,
    get_issue,
    list_dir,
    read_file,
    review,
    submit_initial_review,
    submit_sync_review,
)
from webhook_agent.tools.search_tools import search_files
from webhook_agent.webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.core.agent_definition")

# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "BOT_LOGIN",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "STATIC_REVIEW_INSTRUCTION",
    "SYSTEM_INSTRUCTION",
    "WebhookAgent",
    "_truncate_input_for_tier",
    "_truncate_text_to_token_limit",
    "build_repo_profile_context",
    "build_static_review_instruction",
    "build_transient_turn_content",
    "build_user_message",
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

        # Bounded session service — keeps per-PR conversation history with LRU eviction
        self._session_service = BoundedSessionService()

        # Track current model chain (TPM Descending)
        self._model_chain = get_model_chain()
        self._chain_index = 0
        self._current_model_name = self._model_chain[self._chain_index]
        self._attempted_model_names = {self._normalize_model_name(self._current_model_name)}
        self._fallback_triggered = False

        # Ensure API key is resolved and propagated to env vars before model init
        get_active_api_key()
        self._current_tier: str = _resolve_tier()

        # Model instance for pipeline sub-agents
        model_instance = get_adk_model(
            model_name=self._current_model_name,
            api_key=get_active_api_key(),
        )
        self._sanitizer_plugin = PromptSanitizerPlugin()

        # Streamlined workflow: START -> code_auditor
        # Single-pass high-velocity auditor eliminating redundant sub-agent hops.
        auditor_tc = build_thinking_config(self._current_model_name, conversational=False)
        conv_tc = build_thinking_config(self._current_model_name, conversational=True)

        self._code_auditor = LlmAgent(
            name="code_auditor",
            model=model_instance,
            include_contents="default",
            description="Conducts AST diff-grounded risk audit using Gemini Thinking Mode.",
            instruction=build_static_review_instruction(),
            output_key="code_review_analysis",
            planner=BuiltInPlanner(thinking_config=auditor_tc) if auditor_tc is not None else None,
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                search_files,
                list_dir,
                submit_initial_review,
                submit_sync_review,
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
            planner=BuiltInPlanner(thinking_config=conv_tc) if conv_tc is not None else None,
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                search_files,
                list_dir,
                get_issue,
                create_issue,
            ],
        )

        self._conversational_workflow = Workflow(
            name="conversational_workflow",
            edges=[
                (START, self._conversational_agent),
            ],
        )

        self._iteration_budget_plugin = IterationBudgetPlugin(max_turns=10, max_tool_calls=25)
        self._guardrails_plugin = GuardrailsPlugin(failure_threshold=3, no_progress_window=4)
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
                self._iteration_budget_plugin,
                self._guardrails_plugin,
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
                self._sanitizer_plugin,
            ],
        )

        self._conversational_app = App(
            name=f"{self._app_name}_conversational",
            root_agent=self._conversational_workflow,
            context_cache_config=context_cache_config,
            plugins=[
                self._iteration_budget_plugin,
                self._guardrails_plugin,
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
                self._sanitizer_plugin,
            ],
        )

        # Create the runners
        self._runner = Runner(
            app=self._app,
            session_service=self._session_service,
        )

        self._conversational_runner = Runner(
            app=self._conversational_app,
            session_service=self._session_service,
        )

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        return model_name.replace("models/", "").strip().lower()

    def _advance_model_chain(
        self,
        error: Exception | None = None,
        estimated_tokens: int | None = None,
    ) -> str | None:
        """Cascade once to the next untried model, or return None when exhausted."""
        failed_model = self._normalize_model_name(self._current_model_name)
        self._attempted_model_names.add(failed_model)
        _DEPLETED_MODEL_REGISTRY.mark_depleted(self._current_model_name, error=error)

        if estimated_tokens is not None:
            full_chain = get_model_chain(estimated_tokens=estimated_tokens)
        else:
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
            extra={
                "json_fields": {
                    "event_type": "model_cascade",
                    "from_model": self._current_model_name,
                    "to_model": next_model,
                }
            },
        )
        self._switch_model(next_model)
        return next_model

    def _switch_model(self, next_model: str) -> None:
        """Switch active model across code auditor, conversational agent, workflows, apps, and runners."""
        self._current_model_name = next_model
        self._attempted_model_names.add(self._normalize_model_name(next_model))
        new_model_instance = get_adk_model(
            model_name=next_model,
            api_key=get_active_api_key(),
        )
        auditor_tc = build_thinking_config(next_model, conversational=False)
        conv_tc = build_thinking_config(next_model, conversational=True)
        if hasattr(self, "_code_auditor") and self._code_auditor is not None:
            self._code_auditor.model = new_model_instance
            self._code_auditor.planner = (
                BuiltInPlanner(thinking_config=auditor_tc) if auditor_tc is not None else None
            )
        if hasattr(self, "_conversational_agent") and self._conversational_agent is not None:
            self._conversational_agent.model = new_model_instance
            self._conversational_agent.planner = (
                BuiltInPlanner(thinking_config=conv_tc) if conv_tc is not None else None
            )

        # Dynamically toggle context caching based on model family:
        # Strictly enable only for Gemini 3+ models; disable for Gemma / legacy models.
        cache_cfg = (
            ContextCacheConfig(
                min_tokens=4096,
                ttl_seconds=1800,
                cache_intervals=10,
            )
            if is_gemini_3_plus(next_model)
            else None
        )

        # ADK 2.0+ Workflow clones its node agents into wf.graph.nodes at construction
        # time, so mutating self._code_auditor.model alone leaves the compiled Workflow
        # graph pinned to the initial model. Rebuild Workflow + App + Runner so model
        # routing and 429/503 failover take effect on live executions.
        # Session state is owned by self._session_service (preserved across rebuilds),
        # so rebuilding the stateless Workflow/App/Runner graph between turns or retries
        # never drops or disrupts ongoing session history.
        if hasattr(self, "_code_auditor") and self._code_auditor is not None:
            self._agent = Workflow(
                name="webhook_agent",
                edges=[
                    (START, self._code_auditor),
                ],
            )
            if hasattr(self, "_app") and self._app is not None:
                plugins = list(getattr(self._app, "plugins", None) or [])
                app_name = getattr(self._app, "name", getattr(self, "_app_name", "webhook_agent"))
                self._app = App(
                    name=app_name,
                    root_agent=self._agent,
                    context_cache_config=cache_cfg,
                    plugins=plugins,
                )

        if hasattr(self, "_conversational_agent") and self._conversational_agent is not None:
            self._conversational_workflow = Workflow(
                name="conversational_workflow",
                edges=[
                    (START, self._conversational_agent),
                ],
            )
            if hasattr(self, "_conversational_app") and self._conversational_app is not None:
                conv_plugins = list(getattr(self._conversational_app, "plugins", None) or [])
                conv_app_name = getattr(
                    self._conversational_app,
                    "name",
                    f"{getattr(self, '_app_name', 'webhook_agent')}_conversational",
                )
                self._conversational_app = App(
                    name=conv_app_name,
                    root_agent=self._conversational_workflow,
                    context_cache_config=cache_cfg,
                    plugins=conv_plugins,
                )

        runner_cls = Runner
        if hasattr(self, "_app") and self._app is not None:
            self._runner = runner_cls(
                app=self._app,
                session_service=self._session_service,
            )
        if hasattr(self, "_conversational_app") and self._conversational_app is not None:
            self._conversational_runner = runner_cls(
                app=self._conversational_app,
                session_service=self._session_service,
            )

    def _derive_session_id(self, event_data: dict[str, Any]) -> str:
        """Derive a session ID from the event data for conversation continuity.

        Uses repo_full_name + issue/PR number so that follow-up comments
        on the same thread share a session. Fast-fails without fallback.
        """
        repo = event_data.get("repository", {})
        repo_name = repo.get("full_name") if isinstance(repo, dict) else None
        if not repo_name or repo_name == "unknown":
            repo_name = event_data.get("repo_name")
        if not repo_name or repo_name == "unknown":
            raise ValueError("Cannot derive session ID: event is missing repository full_name")

        raw = event_data.get("raw_payload", {})
        issue = raw.get("issue", {}) if isinstance(raw, dict) else {}
        pr = raw.get("pull_request", {}) if isinstance(raw, dict) else {}
        number = (issue.get("number") if isinstance(issue, dict) else None) or (
            pr.get("number") if isinstance(pr, dict) else None
        )
        if not number:
            raise ValueError(
                f"Cannot derive session ID: event for repository '{repo_name}' is missing PR/issue number"
            )
        return f"{repo_name}/{number}"

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
