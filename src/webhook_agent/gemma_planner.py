"""Gemma 4 planner that converts webhook events into function calls.

This module uses the Gemini Interactions API via `google-genai` to decide
which agent tools to call. It is intentionally small and conservative:

- Uses Gemma 4 models (`gemma-4-31b-it` by default)
- Emits tool calls only; the worker executes them behind policy gates
- Falls back to no-op planning when no API key is available

Environment variables:
- GEMINI_API_KEY or GOOGLE_API_KEY: required to enable the planner
- GEMMA_MODEL: override the model name (default: gemma-4-31b-it)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from google import genai

logger = logging.getLogger("gemma_planner")


GEMMA_MODEL_DEFAULT = "gemma-4-31b-it"


@dataclass
class PlannedAction:
    tool: str
    args: dict[str, Any]
    call_id: str | None = None


def _tool_declarations() -> list[dict[str, Any]]:
    """Tool declarations for the agent core.

    Keep the schema focused on the small set of actions the agent can safely
    perform. The worker validates again before execution.
    """
    return [
        {
            "type": "function",
            "name": "create_issue",
            "description": "Create a new issue in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                },
                "required": ["title"],
            },
        },
        {
            "type": "function",
            "name": "add_comment",
            "description": "Add a comment to an issue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["issue_number", "body"],
            },
        },
        {
            "type": "function",
            "name": "create_branch_commit",
            "description": "Create a branch and add or update a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string"},
                    "base_branch": {"type": "string"},
                    "file_path": {"type": "string"},
                    "file_content": {"type": "string"},
                },
                "required": ["branch_name", "file_path", "file_content"],
            },
        },
        {
            "type": "function",
            "name": "open_pr",
            "description": "Open a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head_branch": {"type": "string"},
                    "base_branch": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["head_branch", "base_branch", "title"],
            },
        },
        {
            "type": "function",
            "name": "add_review_comment",
            "description": "Leave a review-style comment or fallback comment on a PR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["pr_number", "body"],
            },
        },
        {
            "type": "function",
            "name": "merge_pr",
            "description": "Merge a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer"},
                    "merge_method": {
                        "type": "string",
                        "enum": ["merge", "squash", "rebase"],
                    },
                },
                "required": ["pr_number"],
            },
        },
        {
            "type": "function",
            "name": "add_label",
            "description": "Add labels to an issue or repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {"type": "integer"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["labels"],
            },
        },
        {
            "type": "function",
            "name": "assign_reviewers",
            "description": "Request reviewers on a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer"},
                    "reviewers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["pr_number", "reviewers"],
            },
        },
    ]


class GemmaPlanner:
    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model or os.environ.get("GEMMA_MODEL", GEMMA_MODEL_DEFAULT)
        self.client = genai.Client(api_key=api_key)

    @classmethod
    def from_env(cls) -> GemmaPlanner | None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return None
        return cls(api_key=api_key)

    def plan(
        self, event: dict[str, Any], repo_full_name: str, trace_id: str
    ) -> list[PlannedAction]:
        """Ask Gemma 4 to decide which agent tools to call.

        The model should return function calls only for concrete actions. If no
        action is required, it can answer in text and we will treat that as no-op.
        """
        tools = _tool_declarations()
        prompt = (
            "You are the planner for a GitHub App agent.\n"
            f"Trace ID: {trace_id}\n"
            f"Repository: {repo_full_name}\n"
            "Event payload (JSON):\n"
            f"{json.dumps(event, indent=2, sort_keys=True)}\n\n"
            "Decide which function(s) to call. Only call tools for concrete, "
            "safe actions that are directly justified by the payload. If no "
            "action is needed, you may answer with a brief text explanation."
        )

        interaction = self.client.interactions.create(
            model=self.model,
            input=prompt,
            tools=tools,
        )

        planned: list[PlannedAction] = []
        for step in interaction.steps:
            if step.type != "function_call":
                continue
            args = step.arguments
            if not isinstance(args, dict):
                args = {}
            planned.append(
                PlannedAction(
                    tool=step.name, args=args, call_id=getattr(step, "id", None)
                )
            )

        if interaction.output_text:
            logger.info(
                "gemma planner text trace=%s model=%s text=%s",
                trace_id,
                self.model,
                interaction.output_text,
            )

        logger.info(
            "gemma planner trace=%s model=%s planned_actions=%s",
            trace_id,
            self.model,
            [p.tool for p in planned],
        )
        return planned
