"""Standalone Autonomous Feature Developer ADK Agent definition.

Uses FEATURE_AGENT_FREE_KEY for complete API quota isolation, Gemini Thinking Budget
for pre-implementation planning, and a multi-agent SequentialAgent + LoopAgent pipeline
with custom EscalationChecker, GuardrailsPlugin, ContextCacheConfig, and Memory Bank sync.
"""

from __future__ import annotations

import datetime
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any, Literal

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig, ResumabilityConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events import Event, EventActions
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.genai import types
from pydantic import BaseModel, Field

from feature_agent.guardrails import exfil_guard, permission_guard, policies_guard
from feature_agent.memory import (
    auto_capture_callback,
    search_memory_bank,
    skill_curator_callback,
)
from feature_agent.plugins import GuardrailsPlugin
from feature_agent.tools import (
    commit_and_push,
    replace_file_content,
    run_linter,
    run_pytest,
    search_codebase,
    view_file,
)

logger = logging.getLogger("feature_agent.agent")


# --- Item 3: Pydantic Feedback Output Schema ---
class Feedback(BaseModel):
    """Evaluation result schema for verifying code quality and unit test status."""

    grade: Literal["pass", "fail"] = Field(
        description="'pass' if unit tests and linter pass 100%, 'fail' if revisions/fixes are needed."
    )
    comment: str = Field(
        description="Detailed explanation of the test/linter results and remaining issues."
    )
    follow_up_actions: list[str] | None = Field(
        default=None,
        description="Specific repair actions required to fix failing tests.",
    )


# --- Item 2: Custom EscalationChecker Agent ---
class EscalationChecker(BaseAgent):
    """Checks verification evaluation and escalates to break out of the LoopAgent when grade is 'pass'."""

    def __init__(self, name: str = "escalation_checker"):
        super().__init__(name=name)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        evaluation_result = ctx.session.state.get("feature_evaluation")
        if evaluation_result and evaluation_result.get("grade") == "pass":
            logger.info(
                "[%s] Verification evaluation passed. Escalating to break verification loop.",
                self.name,
            )
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            logger.info(
                "[%s] Verification evaluation incomplete or failed. Verification loop continues.",
                self.name,
            )
            yield Event(author=self.name)


# --- Item 16: Volatile <system-reminder> Tail Injection Callback ---
def reminder_injection_callback(
    callback_context: CallbackContext, llm_request: Any
) -> None:
    """Inject volatile date/status tail as a trailing <system-reminder> Content."""
    try:
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        halt_reason = callback_context.state.get("halt_reason", "")
        reminder_text = (
            f"<system-reminder>\n"
            f"Current Time: {now_str}\n"
            f"Active Halt Status: {halt_reason or 'None'}\n"
            f"</system-reminder>"
        )
        if hasattr(llm_request, "contents") and isinstance(llm_request.contents, list):
            llm_request.contents.append(
                types.Content(role="user", parts=[types.Part(text=reminder_text)])
            )
    except Exception as exc:
        logger.debug("Reminder injection skipped: %s", exc)


# --- Item 18: Signed Artifact URL Redaction Callback ---
def redact_artifact_urls_callback(
    callback_context: CallbackContext, llm_request: Any
) -> None:
    """Redact signed blob URLs from model view to prevent credential leakage."""
    try:
        if hasattr(llm_request, "contents") and isinstance(llm_request.contents, list):
            for content in llm_request.contents:
                for part in getattr(content, "parts", []) or []:
                    if (
                        getattr(part, "text", None)
                        and "storage.googleapis.com" in part.text
                    ):
                        part.text = part.text.replace(
                            "storage.googleapis.com", "[REDACTED_BLOB_HOST]"
                        )
    except Exception as exc:
        logger.debug("Artifact URL redaction skipped: %s", exc)


def get_feature_agent_key() -> str:
    """Retrieve dedicated FEATURE_AGENT_FREE_KEY for strict GCP project & quota isolation."""
    key = (
        os.getenv("FEATURE_AGENT_FREE_KEY") or os.getenv("FEATURE_AGENT_PAID_KEY") or ""
    ).strip()
    if not key:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return (
                os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY")
                or "pytest_feature_key"
            )
        raise RuntimeError(
            "CRITICAL ISOLATION ERROR: Missing required secret 'FEATURE_AGENT_FREE_KEY'. "
            "Feature Agent must run on its own isolated GCP project and API key."
        )
    return key


# --- Item 1: Multi-Agent Pipeline Construction ---
def build_feature_developer_agent() -> BaseAgent:
    """Construct the full 4-stage SequentialAgent + LoopAgent pipeline."""
    api_key = get_feature_agent_key()
    model_name = os.getenv("FEATURE_AGENT_MODEL", "gemini-3.5-flash-lite")
    _gcp_project = (
        os.getenv("FEATURE_AGENT_GCP_PROJECT")
        or os.getenv("FEATURE_AGENT_PROJECT")
        or "gen-lang-client-0613181237"
    )

    model_client = Gemini(
        model=model_name,
        client_kwargs={"api_key": api_key},
    )

    planner_agent = LlmAgent(
        name="planner_agent",
        model=model_client,
        description="Analyzes specifications and uses thinking budget to draft surgical edit plans.",
        instruction="Search the codebase, locate target files, and plan surgical replacements.",
        planner=BuiltInPlanner(
            thinking_config=types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=-1,
            )
        ),
        before_agent_callback=skill_curator_callback,
        before_model_callback=redact_artifact_urls_callback,
        after_agent_callback=auto_capture_callback,
        tools=[search_codebase, view_file, search_memory_bank],
        output_key="feature_plan",
    )

    developer_agent = LlmAgent(
        name="developer_agent",
        model=model_client,
        description="Applies surgical file replacements inside the isolated Git Worktree.",
        instruction="Execute planned line replacements using replace_file_content.",
        before_tool_callback=exfil_guard,
        after_agent_callback=auto_capture_callback,
        tools=[replace_file_content, view_file],
        output_key="code_edits",
    )

    # Item 4: Agent boundary scoping on evaluator_agent
    evaluator_agent = LlmAgent(
        name="evaluator_agent",
        model=model_client,
        description="Runs pytest and linter to evaluate feature code quality.",
        instruction="Run pytest and linter. Grade 'pass' if 100% clean, otherwise grade 'fail'.",
        before_tool_callback=policies_guard,
        tools=[run_pytest, run_linter],
        output_schema=Feedback,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        output_key="feature_evaluation",
    )

    debugger_agent = LlmAgent(
        name="debugger_agent",
        model=model_client,
        description="Fixes failing test assertions and linter errors.",
        instruction="Review failure feedback and apply targeted code fixes.",
        before_tool_callback=permission_guard,
        tools=[replace_file_content, run_pytest],
        output_key="debug_edits",
    )

    verification_loop = LoopAgent(
        name="verification_loop",
        max_iterations=5,
        sub_agents=[
            evaluator_agent,
            EscalationChecker(name="escalation_checker"),
            debugger_agent,
        ],
    )

    pr_composer_agent = LlmAgent(
        name="pr_composer_agent",
        model=model_client,
        description="Commits verified work and pushes branch to origin.",
        instruction="Call commit_and_push to record the verified feature branch.",
        tools=[commit_and_push],
        output_key="final_commit",
    )

    return SequentialAgent(
        name="feature_developer_agent",
        sub_agents=[
            planner_agent,
            developer_agent,
            verification_loop,
            pr_composer_agent,
        ],
    )


def build_feature_app() -> App:
    """Construct the full ADK App with plugins, caching, compaction, and resumability."""
    agent = build_feature_developer_agent()
    api_key = get_feature_agent_key()
    summarizer_client = Gemini(
        model="gemini-3.5-flash-lite",
        client_kwargs={"api_key": api_key},
    )
    return App(
        name="feature_developer_app",
        root_agent=agent,
        plugins=[GuardrailsPlugin(max_repeated_failures=3)],
        context_cache_config=ContextCacheConfig(ttl_seconds=3600),
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=15,
            overlap_size=2,
            summarizer=LlmEventSummarizer(llm=summarizer_client),
            token_threshold=750_000,
            event_retention_size=20,
        ),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
