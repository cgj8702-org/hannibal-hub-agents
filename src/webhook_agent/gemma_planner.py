"""Gemma 4 planner that converts canonical webhook events into structured tool calls.

This module uses the Gemini Interactions API via `google-genai` to decide
which agent tools to call. It is intentionally small and conservative:

- Uses Gemma 4 models (``gemma-4-31b-it`` by default)
- Emits tool calls only; the worker executes them behind policy gates
- Falls back to no-op planning when no API key is available
- Supports event-type-aware prompting: the model receives the canonical event
  category and a tight schema of allowed actions for that event class

Environment variables:
- GEMINI_API_KEY or GOOGLE_API_KEY: required to enable the planner
- GEMMA_MODEL: override the model name (default: gemma-4-31b-it)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from google import genai

logger = logging.getLogger("gemma_planner")

GEMMA_MODEL_DEFAULT = "gemma-4-31b-it"


# ---------------------------------------------------------------------------
# Canonical event type (mirror of agent_core.CanonicalEvent for import safety)
# ---------------------------------------------------------------------------
@dataclass
class PlannerEvent:
    """Lightweight event representation for the planner."""

    canonical: str  # e.g. "pull_request.opened"
    delivery_id: str
    event_name: str
    action: str | None
    sender: dict[str, Any] | None
    installation: dict[str, Any] | None
    repository: dict[str, Any] | None
    raw_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlannerEvent:
        canonical = (
            d.get("canonical", "") or f"{d.get('event_name', '')}.{d.get('action', '')}"
        )
        if canonical.endswith("."):
            canonical = canonical.rstrip(".")
        return cls(
            canonical=canonical,
            delivery_id=d.get("delivery_id", "unknown"),
            event_name=d.get("event_name", "unknown"),
            action=d.get("action"),
            sender=d.get("sender"),
            installation=d.get("installation"),
            repository=d.get("repository"),
            raw_payload=d.get("raw_payload", {}),
        )


@dataclass
class PlannedAction:
    tool: str
    args: dict[str, Any]
    call_id: str | None = None


# ---------------------------------------------------------------------------
# Event-class → allowed tool schemas
# ---------------------------------------------------------------------------
def _common_schemas() -> list[dict[str, Any]]:
    """Tool schemas available across all event classes."""
    return [
        {
            "type": "function",
            "name": "add_comment",
            "description": "Add a general comment to an issue or pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {
                        "type": "integer",
                        "description": "Issue or PR number",
                    },
                    "body": {
                        "type": "string",
                        "description": "Comment body (Markdown)",
                    },
                },
                "required": ["issue_number", "body"],
            },
        },
        {
            "type": "function",
            "name": "add_label",
            "description": "Add labels to an issue, PR, or repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {
                        "type": "integer",
                        "description": "Optional issue/PR number",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of label names to add",
                    },
                },
                "required": ["labels"],
            },
        },
    ]


def _pr_review_schemas() -> list[dict[str, Any]]:
    """Tool schemas for PR review workflows."""
    return [
        {
            "type": "function",
            "name": "add_review_comment",
            "description": "Leave a general review-style comment or fallback comment on a PR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "body": {
                        "type": "string",
                        "description": "Review comment body (Markdown)",
                    },
                },
                "required": ["pr_number", "body"],
            },
        },
        {
            "type": "function",
            "name": "reply_to_review_comment",
            "description": "Reply to a specific review comment on a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "comment_id": {
                        "type": "integer",
                        "description": "Review comment ID to reply to",
                    },
                    "body": {"type": "string", "description": "Reply body (Markdown)"},
                },
                "required": ["pr_number", "comment_id", "body"],
            },
        },
        {
            "type": "function",
            "name": "submit_review",
            "description": "Submit a formal review on a pull request with an event type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "body": {"type": "string", "description": "Review body (Markdown)"},
                    "event": {
                        "type": "string",
                        "enum": ["APPROVE", "COMMENT", "REQUEST_CHANGES"],
                        "description": "Review event type",
                    },
                },
                "required": ["pr_number", "body"],
            },
        },
        {
            "type": "function",
            "name": "assign_reviewers",
            "description": "Request reviewers on a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "reviewers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of GitHub usernames to request as reviewers",
                    },
                },
                "required": ["pr_number", "reviewers"],
            },
        },
    ]


def _pr_lifecycle_schemas() -> list[dict[str, Any]]:
    """Tool schemas for PR lifecycle management."""
    return [
        {
            "type": "function",
            "name": "open_pr",
            "description": "Open a new pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head_branch": {
                        "type": "string",
                        "description": "Head branch name",
                    },
                    "base_branch": {
                        "type": "string",
                        "description": "Base branch name",
                    },
                    "title": {"type": "string", "description": "Pull request title"},
                    "body": {
                        "type": "string",
                        "description": "Pull request body (Markdown)",
                    },
                },
                "required": ["head_branch", "base_branch", "title"],
            },
        },
        {
            "type": "function",
            "name": "merge_pr",
            "description": "Merge a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "merge_method": {
                        "type": "string",
                        "enum": ["merge", "squash", "rebase"],
                        "description": "Merge method (default: merge)",
                    },
                },
                "required": ["pr_number"],
            },
        },
        {
            "type": "function",
            "name": "create_branch_commit",
            "description": "Create a new branch (from base or default) and add or update a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": "New branch name"},
                    "base_branch": {
                        "type": "string",
                        "description": "Base branch (defaults to repo default)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path to create or update",
                    },
                    "file_content": {"type": "string", "description": "File content"},
                },
                "required": ["branch_name", "file_path", "file_content"],
            },
        },
        {
            "type": "function",
            "name": "get_pr_diff",
            "description": "Fetch the file diffs and changed files in a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                },
                "required": ["pr_number"],
            },
        },
        {
            "type": "function",
            "name": "update_pr_description",
            "description": "Update a pull request's description (body), title, or mark it as ready for review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                    "body": {
                        "type": "string",
                        "description": "New pull request description (Markdown)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional new pull request title",
                    },
                    "ready_for_review": {
                        "type": "boolean",
                        "description": "Set to true to transition draft pull requests to ready-for-review",
                    },
                },
                "required": ["pr_number"],
            },
        },
    ]


def _issue_schemas() -> list[dict[str, Any]]:
    """Tool schemas for issue management."""
    return [
        {
            "type": "function",
            "name": "create_issue",
            "description": "Create a new issue in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body (Markdown)"},
                },
                "required": ["title"],
            },
        },
    ]


def _tool_declarations_for_event(canonical: str) -> list[dict[str, Any]]:
    """Return the tool schema set appropriate for a given canonical event type.

    Keeps the schema tight and action-oriented — only the tools relevant for
    the event class are exposed to the model.
    """
    tools: list[dict[str, Any]] = []
    tools.extend(_common_schemas())

    if canonical.startswith("pull_request."):
        tools.extend(_pr_review_schemas())
        tools.extend(_pr_lifecycle_schemas())
        tools.extend(_issue_schemas())
    elif canonical.startswith("issue_comment."):
        tools.extend(_issue_schemas())
    elif canonical.startswith("pull_request_review_comment."):
        tools.extend(_pr_review_schemas())
    elif canonical.startswith("pull_request_review."):
        tools.extend(_pr_review_schemas())
        tools.extend(_pr_lifecycle_schemas())
    elif canonical == "pull_request_review_requested":
        tools.extend(_pr_review_schemas())
    elif canonical.startswith("label."):
        pass  # labels are read-only for now
    elif canonical.startswith("installation."):
        pass  # installation events are read-only

    return tools


def _build_event_prompt(event: PlannerEvent, trace_id: str) -> str:
    """Build a structured prompt for the planner.

    Incorporates the schema-first review style from the code_reviewer.py prototype:
    - Explicitly enumerates context (repo, event, sender)
    - Emphasizes structured, deterministic outputs
    - Restricts action set per event class
    """
    repo_info = ""
    if event.repository:
        repo_info = (
            f"Repository: {event.repository.get('full_name', 'unknown')}\n"
            f"Repo Owner: {event.repository.get('owner', {}).get('login', 'unknown')}\n"
        )

    sender_info = ""
    if event.sender:
        sender_info = (
            f"Sender: {event.sender.get('login', 'unknown')}\n"
            f"Sender Type: {event.sender.get('type', 'unknown')}\n"
        )

    # Extract key identifiers from the raw payload for context
    raw = event.raw_payload
    extra_context = ""
    if event.canonical.startswith("pull_request."):
        pr = raw.get("pull_request", {})
        extra_context = (
            f"PR Number: {pr.get('number', 'unknown')}\n"
            f"PR Title: {pr.get('title', 'N/A')}\n"
            f"PR Head Branch: {pr.get('head', {}).get('ref', 'N/A')}\n"
            f"PR Base Branch: {pr.get('base', {}).get('ref', 'N/A')}\n"
        )
    elif event.canonical.startswith("issue_comment."):
        issue = raw.get("issue", {})
        comment = raw.get("comment", {})
        extra_context = (
            f"Issue/PR Number: {issue.get('number', 'unknown')}\n"
            f"Comment Body Snippet: {comment.get('body', '')[:200]}\n"
        )
    elif event.canonical.startswith("pull_request_review_comment."):
        comment = raw.get("comment", {})
        extra_context = (
            f"Review Comment ID: {comment.get('id', 'unknown')}\n"
            f"Comment Body Snippet: {comment.get('body', '')[:200]}\n"
        )
    elif event.canonical.startswith("pull_request_review."):
        review = raw.get("review", {})
        extra_context = (
            f"Review State: {review.get('state', 'N/A')}\n"
            f"Review Body Snippet: {review.get('body', '')[:200]}\n"
        )

    # Check for injected diff/template context
    if "pr_diff" in raw and "pr_template" in raw:
        extra_context += (
            f"\n=== PR CODE DIFF ===\n{raw['pr_diff']}\n"
            f"\n=== PR TEMPLATE TO FILL OUT ===\n{raw['pr_template']}\n"
            "\nINSTRUCTION:\n"
            "The user used `/create` in the pull request description.\n"
            "You MUST parse the PR Code Diff above, fill out the provided PR Template completely with a detailed explanation of the changes, "
            "and output a call to the `update_pr_description` tool containing the filled-out template in the `body` argument, "
            "setting `ready_for_review` to true.\n"
        )

    return (
        "You are the planner for a GitHub App agent. Your role is to decide "
        "which tool(s) to call based on the incoming webhook event.\n\n"
        f"Trace ID: {trace_id}\n"
        f"Canonical Event: {event.canonical}\n"
        f"Delivery ID: {event.delivery_id}\n"
        f"{repo_info}"
        f"{sender_info}"
        f"{extra_context}"
        "\n---\n"
        "Rules:\n"
        "1. Only call tools from the provided schema set.\n"
        "2. Do NOT invent tools or parameters.\n"
        "3. If no action is needed, respond in text explaining why.\n"
        "4. Keep arguments concise and correct.\n"
        "5. For PR review events, prefer submit_review over add_review_comment "
        "when a formal review is appropriate.\n"
    )


# ---------------------------------------------------------------------------
# GemmaPlanner class
# ---------------------------------------------------------------------------
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
        self,
        event: dict[str, Any] | PlannerEvent,
        trace_id: str,
    ) -> list[PlannedAction]:
        """Ask Gemma 4 to decide which agent tools to call for this event.

        Accepts either a raw dict (legacy) or a PlannerEvent. The model should
        return function calls only for concrete actions. If no action is required,
        it can answer in text and we will treat that as no-op.
        """
        # Normalize to PlannerEvent
        if isinstance(event, dict):
            planner_event = PlannerEvent.from_dict(event)
        else:
            planner_event = event

        tools = _tool_declarations_for_event(planner_event.canonical)
        prompt = _build_event_prompt(planner_event, trace_id)

        logger.debug(
            "gemma planner request trace=%s canonical=%s tools=%s",
            trace_id,
            planner_event.canonical,
            [t.get("name") for t in tools],
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
            logger.debug(
                "gemma planner text trace=%s model=%s text=%s",
                trace_id,
                self.model,
                interaction.output_text,
            )

        logger.debug(
            "gemma planner trace=%s model=%s planned_actions=%s",
            trace_id,
            self.model,
            [p.tool for p in planned],
        )
        return planned
