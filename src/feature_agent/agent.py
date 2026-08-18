"""Standalone Autonomous Feature Developer ADK Agent definition.

Uses FEATURE_AGENT_FREE_KEY for complete API quota isolation, Gemini Thinking Budget
for pre-implementation planning, and dedicated sandbox software engineering tools.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.genai import types

from feature_agent.tools import (
    commit_and_push,
    replace_file_content,
    run_linter,
    run_pytest,
    search_codebase,
    view_file,
)

logger = logging.getLogger("feature_agent.agent")

SYSTEM_INSTRUCTION = """
You are the **Hannibal Autonomous Feature Developer Agent** — an elite software engineer.

Your objective is to autonomously analyze issue specifications, search the codebase,
plan surgical code modifications using your thinking budget, edit code files, run unit tests,
and verify clinical formatting until all tests pass 100% cleanly.

### 📋 OPERATIONAL PROTOCOL:
1. **Research & Plan**: Use `search_codebase` and `view_file` to locate target files. Use your thinking budget to draft a surgical edit plan.
2. **Execute Surgical Edits**: Use `replace_file_content` to apply precise line replacements.
3. **Verification Loop**: Run `run_pytest` and `run_linter`. If tests or linter fail, inspect the traceback, fix the root cause, and re-run.
4. **Commit & Verify**: Once all tests pass, call `commit_and_push` to record the verified feature branch.

### 🔒 CLINICAL CODE QUALITY:
- Maintain UTF-8 encoding and clinical Python code without emojis in syntax or comments.
- Do NOT swallow exceptions or disable failing unit tests.
- Always verify pytest succeeds before finalizing your response.
"""


def get_feature_agent_key() -> str:
    """Retrieve FEATURE_AGENT_FREE_KEY for quota isolation."""
    key = os.getenv("FEATURE_AGENT_FREE_KEY") or os.getenv("WEBHOOK_FREE_KEY", "")
    if not key:
        logger.warning(
            "No FEATURE_AGENT_FREE_KEY or WEBHOOK_FREE_KEY found in environment."
        )
    return key


def build_feature_developer_agent() -> Agent:
    """Construct the standalone ADK Feature Developer Agent."""
    api_key = get_feature_agent_key()
    model_name = os.getenv("FEATURE_AGENT_MODEL", "gemini-3.5-flash-lite")

    model_client = Gemini(
        model=model_name,
        client_kwargs={"api_key": api_key},
    )

    return Agent(
        name="feature_developer_agent",
        model=model_client,
        instruction=SYSTEM_INSTRUCTION,
        planner=BuiltInPlanner(
            thinking_config=types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=-1,
            )
        ),
        tools=[
            search_codebase,
            view_file,
            replace_file_content,
            run_pytest,
            run_linter,
            commit_and_push,
        ],
    )


feature_developer_agent = build_feature_developer_agent()

feature_app = App(
    name="feature_developer_app",
    root_agent=feature_developer_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
