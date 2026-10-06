"""WebhookAgent ADK agent definition and execution orchestration.

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from github import Github
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.runners import RunConfig, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Workflow
from google.genai import types as genai_types

from webhook_agent.bot_identity import _is_bot_event
from webhook_agent.callbacks import (
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
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
    get_shared_genai_client,
    run_in_bg_loop,
)
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
from webhook_agent.memory_service import InMemoryMemoryService
from webhook_agent.models.model_chain import (
    _DEPLETED_MODEL_REGISTRY,
    _is_transient_error,
    _select_model_for_event,
    get_active_model,
    is_gemini_3_plus,
)
from webhook_agent.models.model_factory import get_adk_model
from webhook_agent.models.rate_limiter import (
    extract_rate_limit_details,
    get_active_api_key,
    rpm_waiter,
)
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.verdict_parser import calculate_verdict
from webhook_agent.review.writeback_policy import (
    _COMMENT_RATE_LIMITER,
    _is_formal_review_eligible,
    evaluate_writeback_policy,
)
from webhook_agent.sanitizer_plugin import PromptSanitizerPlugin
from webhook_agent.state.review_checkpoint import review_checkpoint_manager
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
        from webhook_agent import webhook_agent as wa_mod
        from webhook_agent.models.model_chain import get_model_chain as default_get_chain

        get_chain_fn = getattr(wa_mod, "get_model_chain", default_get_chain) or default_get_chain
        self._model_chain = get_chain_fn()
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
        from webhook_agent import webhook_agent as wa_mod

        failed_model = self._normalize_model_name(self._current_model_name)
        self._attempted_model_names.add(failed_model)
        depleted_registry = getattr(wa_mod, "_DEPLETED_MODEL_REGISTRY", _DEPLETED_MODEL_REGISTRY)
        depleted_registry.mark_depleted(self._current_model_name, error=error)

        from webhook_agent.models.model_chain import get_model_chain as default_get_chain

        get_chain_fn = getattr(wa_mod, "get_model_chain", default_get_chain) or default_get_chain
        full_chain = get_chain_fn()
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

        runner_cls = getattr(wa_mod, "Runner", Runner)
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
        repo_full_name = (
            event_data.get("repository", {}).get("full_name")
            if event_data.get("repository")
            else "unknown"
        )
        canonical = event_data.get("canonical", "")

        # Check writeback policy (bot-authored, read-only, closed PR, mutations disabled, dry-run)
        policy_actions = evaluate_writeback_policy(
            event_data=event_data,
            dry_run=self.dry_run,
            trace_id=trace_id,
            is_bot_event_fn=_is_bot_event,
        )
        if policy_actions is not None:
            return policy_actions

        logger.debug(
            "✅ All policy checks passed, building session context (trace: %s)",
            trace_id[-4:],
        )

        raw = event_data.get("raw_payload", {})

        # Derive session and user IDs
        session_id = self._derive_session_id(event_data)
        sender = event_data.get("sender") or {}
        sender_login = sender.get("login", "")
        user_id = sender_login or "anonymous"

        logger.debug(
            "👤 Session context: session_id=%s, user_id=%s",
            session_id,
            user_id,
        )

        # Build the user message
        user_message = self._build_user_message(event_data)
        logger.debug(
            "📝 Built user message for agent (length: %d chars)",
            len(getattr(user_message.parts[0], "text", "") or "") if user_message.parts else 0,
        )

        # Select model tier dynamically for this event
        selected_model = _select_model_for_event(event_data)
        if self._current_model_name != selected_model:
            logger.info(
                "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
                selected_model,
                canonical,
                trace_id[-4:],
            )
            self._current_model_name = selected_model
            new_model_instance = get_adk_model(
                model_name=selected_model,
                api_key=get_active_api_key(),
            )
            if hasattr(self, "_code_auditor") and self._code_auditor is not None:
                self._code_auditor.model = new_model_instance
            if hasattr(self, "_conversational_agent") and self._conversational_agent is not None:
                self._conversational_agent.model = new_model_instance

        comment_body = (
            (raw.get("comment", {}) or {}).get("body", "") if isinstance(raw, dict) else ""
        )
        is_pr_review_event = _is_formal_review_eligible(canonical, comment_body)
        active_runner = self._runner if is_pr_review_event else self._conversational_runner

        # Run the agent asynchronously with retry and fallback support
        results: list[ActionResult] = []
        emitted_texts: list[str] = []
        final_session = None

        async def _execute_agent() -> None:
            max_llm_calls = int(
                os.environ.get("MAX_AUDITOR_LLM_CALLS")
                or os.environ.get("ADK_MAX_LLM_CALLS")
                or "6"
            )
            run_config = RunConfig(max_llm_calls=max_llm_calls)
            try:
                async for event in active_runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=user_message,
                    run_config=run_config,
                ):
                    # Handle token recording if usage metadata is available
                    if hasattr(event, "usage_metadata") and event.usage_metadata:
                        total_tok = getattr(
                            event.usage_metadata, "total_token_count", 0
                        ) or getattr(event.usage_metadata, "total_tokens", 0)
                        if total_tok > 0:
                            await rpm_waiter.record_actual_tokens(
                                model=self._current_model_name,
                                actual_tokens=total_tok,
                            )

                    # Handle function response events — these are tool results from ADK
                    if hasattr(event, "get_function_responses"):
                        responses = event.get_function_responses()
                        if responses:
                            logger.debug(
                                "🔧 Received %d tool responses from ADK",
                                len(responses),
                            )
                            for response in responses:
                                results.append(
                                    ActionResult(
                                        tool=str(response.name or ""),
                                        success=True,
                                        detail=f"tool executed: {response.response}",
                                    )
                                )

                    # Handle text responses — log the agent's reasoning (filtering out thought tokens)
                    if (
                        event.content
                        and event.content.parts
                        and any(
                            hasattr(p, "text") and p.text and not getattr(p, "thought", False)
                            for p in event.content.parts
                        )
                    ):
                        for part in event.content.parts:
                            if getattr(part, "thought", False):
                                continue
                            if hasattr(part, "text") and part.text:
                                emitted_texts.append(part.text)
                                logger.debug(
                                    "💭 Agent response received (trace: %s): %s",
                                    trace_id[-4:],
                                    part.text,
                                )
                                logger.info(
                                    "🧠 Agent response: %s (trace: %s)",
                                    part.text,
                                    trace_id[-4:],
                                )
            except Exception as run_err:
                if "LlmCallsLimitExceededError" in type(run_err).__name__:
                    logger.warning(
                        "🛑 Auditor reached max LLM calls limit (%d calls) for trace %s: %s",
                        max_llm_calls,
                        trace_id[-4:],
                        run_err,
                    )
                else:
                    raise

        changed_files_list = list(raw.get("changed_files") or [])
        deterministic_state = {
            "deterministic_changed_files": changed_files_list,
        }

        pr_number = None
        if isinstance(raw, dict):
            pr_number = (raw.get("pull_request") or {}).get("number") or (
                raw.get("issue") or {}
            ).get("number")
        head_sha = (
            str(
                (raw.get("pull_request") or {}).get("head", {}).get("sha") or raw.get("after") or ""
            )
            if isinstance(raw, dict)
            else ""
        )

        async def _run() -> None:
            nonlocal results, final_session
            last_error = None
            self._attempted_model_names = {self._normalize_model_name(self._current_model_name)}

            # Checkpoint short-circuit: if this exact commit was already reviewed, skip duplicate
            if is_pr_review_event and pr_number and head_sha:
                checkpoint = review_checkpoint_manager.get_checkpoint(
                    repo_full_name, pr_number, head_sha
                )
                if checkpoint and checkpoint.get("status") == "completed":
                    logger.info(
                        "🔒 Checkpoint: PR %s#%d at commit %s already reviewed (completed). Skipping.",
                        repo_full_name,
                        pr_number,
                        head_sha,
                    )
                    results.append(
                        ActionResult(
                            tool="review",
                            success=True,
                            detail=f"PR #{pr_number} already reviewed at commit {head_sha} (checkpoint completed)",
                        )
                    )
                    return

            # Try with retry and optional fallback model
            active_app_name = (
                getattr(getattr(active_runner, "app", None), "name", self._app_name)
                or self._app_name
            )
            for attempt in range(_MAX_RETRIES):
                try:
                    # Ensure session exists before invoking the runner.
                    # In the installed ADK version, InMemorySessionService only
                    # exposes async helpers, so we must await them here.
                    session = await self._session_service.get_session(
                        app_name=active_app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if session is None:
                        await self._session_service.create_session(
                            app_name=active_app_name,
                            user_id=user_id,
                            session_id=session_id,
                            state=deterministic_state,
                        )
                        # Re-fetch the session after creation
                        session = await self._session_service.get_session(
                            app_name=active_app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Created new ADK session %s for user %s",
                            session_id,
                            user_id,
                        )
                    if session is None:
                        raise RuntimeError("Failed to create deterministic ADK session")
                    if any(
                        session.state.get(key) != value
                        for key, value in deterministic_state.items()
                    ):
                        await self._session_service.append_event(
                            session,
                            Event(
                                invocation_id=trace_id,
                                author="webhook_agent_scope_gate",
                                actions=EventActions(state_delta=deterministic_state),
                            ),
                        )
                        session = await self._session_service.get_session(
                            app_name=active_app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )

                    # Deduplication check: if a review was submitted < 30s ago, inject notice
                    if session and session.state:
                        last_review_ts = session.state.get("last_review_timestamp", 0)
                        now = time.time()
                        time_since_review = now - last_review_ts
                        if time_since_review < 30.0:
                            notice = (
                                f"\n\n[⚠️ SYSTEM NOTICE: You submitted a formal PR review {time_since_review:.1f} seconds ago. "
                                "Do NOT call review() again unless explicitly requested by a new /review command.]"
                            )
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + notice

                        previous_critique = session.state.get("last_review_critique", "")
                        if previous_critique:
                            critique_notice = (
                                f"\n\n[YOUR PREVIOUS REVIEW CRITIQUE]:\n{previous_critique}\n"
                                "Verify line-by-line which specific items were resolved by the new commit."
                            )
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + critique_notice

                    # Set user_state values - they get merged into session.state by InMemorySessionService
                    # This is needed because session copies are returned and our direct mutations wouldn't persist
                    user_state_map = self._session_service.user_state.setdefault(
                        active_app_name, {}
                    ).setdefault(user_id, {})
                    user_state_map["gh_client"] = gh_client
                    user_state_map["repo_full_name"] = repo_full_name
                    user_state_map["sender"] = user_id
                    user_state_map["review_mode"] = (
                        "sync" if canonical == "pull_request.synchronize" else "initial"
                    )
                    user_state_map["formal_review_eligible"] = _is_formal_review_eligible(
                        canonical,
                        comment_body,
                    )
                    if event_data.get("deterministic_precompiled_ast"):
                        user_state_map["deterministic_precompiled_ast"] = True
                        tools_exec = user_state_map.setdefault("tools_executed", [])
                        if "verify_python_ast" not in tools_exec:
                            tools_exec.append("verify_python_ast")
                    if is_pr_review_event and pr_number and head_sha:
                        review_checkpoint_manager.save_checkpoint(
                            repo=repo_full_name,
                            pr_number=pr_number,
                            head_sha=head_sha,
                            canonical=canonical,
                            precompiled_dossier=getattr(user_message.parts[0], "text", "")
                            if user_message.parts
                            else "",
                            pr_diff=str(raw.get("pr_diff") or ""),
                            changed_files=changed_files_list,
                            status="pending",
                        )
                    # Execute the ADK runner with current model
                    await _execute_agent()

                    # Fetch the final session state with all sub-agent output_key updates
                    final_session = await self._session_service.get_session(
                        app_name=active_app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    return  # Success - exit the retry loop

                except Exception as e:
                    if type(e).__name__ == "AbortAgentExecution" or "AbortAgentExecution" in str(
                        type(e)
                    ):
                        logger.info(
                            "🔒 Agent execution short-circuited (trace: %s): %s",
                            trace_id[-4:],
                            e,
                        )
                        results.append(
                            ActionResult(
                                tool="skip_closed_pr",
                                success=True,
                                detail=f"Execution short-circuited: {e}",
                            )
                        )
                        return

                    last_error = e
                    if _is_transient_error(e):
                        if pr_number and head_sha:
                            review_checkpoint_manager.mark_rate_limited(
                                repo_full_name, pr_number, head_sha, str(e)
                            )
                    if _is_transient_error(e) and attempt < _MAX_RETRIES - 1:
                        rate_details = extract_rate_limit_details(e)
                        next_model = self._advance_model_chain(error=e)
                        if next_model is None:
                            break
                        err_s = str(e).lower()
                        is_503_high_demand = (
                            "503" in err_s or "unavailable" in err_s or "high demand" in err_s
                        )
                        parsed_retry = rate_details.get("retry_after_seconds")
                        if is_503_high_demand:
                            retry_delay = 0.5
                        elif parsed_retry is not None and parsed_retry > 0:
                            retry_delay = min(float(parsed_retry) + 0.5, 65.0)
                        else:
                            retry_delay = min(2.0 * (attempt + 1), 15.0)
                        logger.warning(
                            "Transient error on attempt %d/%d (trace: %s): %s. Active model failover -> %s (delay: %.1fs)",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            e,
                            self._current_model_name,
                            retry_delay,
                        )
                        if retry_delay > 0:
                            await asyncio.sleep(retry_delay)
                        continue
                    logger.debug(
                        "Non-transient error or exhausted retries: raising exception (trace: %s)",
                        trace_id[-4:],
                    )
                    logger.exception(
                        "ADK agent run failed (trace: %s)",
                        trace_id[-4:],
                    )
                    results.append(
                        ActionResult(
                            tool="plan",
                            success=False,
                            detail=f"ADK agent error: {e}",
                        )
                    )
                    return

            # If we exhausted retries, add error result
            if last_error and _is_transient_error(last_error):
                logger.debug(
                    "All retry attempts exhausted (trace: %s): retrying model was unavailable",
                    trace_id[-4:],
                )
                logger.error(
                    "Model unavailable after %d retries (trace: %s)",
                    _MAX_RETRIES,
                    trace_id[-4:],
                )
                results.append(
                    ActionResult(
                        tool="plan",
                        success=False,
                        detail=f"Model unavailable after {_MAX_RETRIES} retries: {last_error}",
                    )
                )

        # Run the ADK coroutine on the persistent background loop to avoid
        # "Event loop is closed" issues when the process receives signals or
        # when httpx/anyio transports attempt to close transports on a loop
        # that has been shut down. This schedules the coroutine and waits
        # for completion synchronously.
        run_in_bg_loop(_run())

        # Deterministic review submission: if no review tool was called during a PR review event,
        # extract structured audit state from ADK session or emitted texts, and enforce verdict.
        # Forward-fix: also cover issue_comment reconciliation follow-ups that emit
        # resolutions + verdict (e.g. REQUEST_CHANGES -> APPROVE flips). Otherwise
        # conversational re-reviews are silently dropped as "no actions".
        has_review_action = any(r.tool == "review" and r.success for r in results)

        is_comment_reconciliation = False
        if not is_pr_review_event and not has_review_action and emitted_texts:
            full_text = "\n\n".join(emitted_texts)
            has_resolutions = '"resolutions"' in full_text
            has_verdict = '"verdict"' in full_text
            is_pr_comment = canonical.startswith(
                ("issue_comment.", "pull_request_review_comment.", "pull_request_review.")
            )
            if is_pr_comment and has_resolutions and has_verdict:
                is_comment_reconciliation = True
                logger.info(
                    "Comment reconciliation detected (trace: %s): promoting to deterministic review submission",
                    trace_id[-4:],
                )

        if (is_pr_review_event or is_comment_reconciliation) and not has_review_action:
            # 1. Safely extract review payload from session state or emitted text
            review_payload = ""
            if final_session and final_session.state:
                raw_analysis = final_session.state.get("code_review_analysis")
                raw_verdict = final_session.state.get("audit_verdict")

                if raw_analysis:
                    if hasattr(raw_analysis, "model_dump_json"):
                        review_payload = raw_analysis.model_dump_json()
                    elif isinstance(raw_analysis, dict):
                        import json

                        review_payload = json.dumps(raw_analysis)
                    else:
                        review_payload = str(raw_analysis)
                elif raw_verdict:
                    if hasattr(raw_verdict, "model_dump_json"):
                        review_payload = raw_verdict.model_dump_json()
                    elif isinstance(raw_verdict, dict):
                        import json

                        review_payload = json.dumps(raw_verdict)
                    else:
                        review_payload = str(raw_verdict)

            if not review_payload and emitted_texts:
                full_text = "\n\n".join(emitted_texts)
                is_review_content = (
                    "Scorecard" in full_text
                    or "| Category |" in full_text
                    or "Verdict:" in full_text
                    or '"verdict"' in full_text
                    or '"executive_summary"' in full_text
                    or '"critical_issues"' in full_text
                    or '"resolutions"' in full_text
                    or "Code Review" in full_text
                )
                if is_review_content:
                    review_payload = full_text

            if review_payload:
                pr_number = None
                if isinstance(raw, dict):
                    pr_number = (raw.get("pull_request") or {}).get("number") or (
                        raw.get("issue") or {}
                    ).get("number")

                if pr_number:
                    try:
                        repo = gh_client.get_repo(repo_full_name)
                        pr = repo.get_pull(pr_number)
                        detail, submitted = _submit_formal_review(
                            pr,
                            review_payload,
                            "COMMENT",
                            f"{repo_full_name}#{pr_number}",
                            {
                                "review_mode": (
                                    "sync" if canonical == "pull_request.synchronize" else "initial"
                                )
                            },
                        )
                        results.append(
                            ActionResult(
                                tool="review",
                                success=submitted,
                                detail=detail,
                            )
                        )
                        logger.info("Deterministic review result for PR #%d: %s", pr_number, detail)
                        if submitted and head_sha:
                            review_checkpoint_manager.mark_completed(
                                repo_full_name, pr_number, head_sha
                            )
                    except Exception as fallback_err:
                        logger.warning(
                            "Deterministic review submission failed: %s",
                            fallback_err,
                        )

        elif not is_pr_review_event and not is_comment_reconciliation:
            if emitted_texts:
                full_reply = "\n\n".join(emitted_texts).strip()
                if full_reply:
                    pr_number = None
                    if isinstance(raw, dict):
                        pr_number = (raw.get("pull_request") or {}).get("number") or (
                            raw.get("issue") or {}
                        ).get("number")
                    if pr_number:
                        target_key = f"{repo_full_name}#{pr_number}"
                        if _COMMENT_RATE_LIMITER.is_allowed(target_key):
                            try:
                                repo = gh_client.get_repo(repo_full_name)
                                issue = repo.get_issue(pr_number)
                                comment_obj = issue.create_comment(full_reply)
                                _COMMENT_RATE_LIMITER.record(target_key)
                                results.append(
                                    ActionResult(
                                        tool="add_comment",
                                        success=True,
                                        detail=f"Posted conversational comment to #{pr_number}: {getattr(comment_obj, 'html_url', 'OK')}",
                                    )
                                )
                                logger.info(
                                    "💬 Posted conversational comment to %s#%d (trace: %s)",
                                    repo_full_name,
                                    pr_number,
                                    trace_id[-4:],
                                )
                            except Exception as comment_err:
                                logger.warning(
                                    "Failed to post conversational comment to #%d: %s",
                                    pr_number,
                                    comment_err,
                                )
                        else:
                            logger.warning(
                                "Conversational comment rate limited for %s (trace: %s)",
                                target_key,
                                trace_id[-4:],
                            )

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id[-4:],
            )

        return results
