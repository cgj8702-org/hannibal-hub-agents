"""Sequential Thinking tool for Webhook Agent.

Enables step-by-step reasoning, hypothesis generation, revision, and multi-thought validation.
Allows the auditor agent to reason dynamically before emitting final review findings.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools import FunctionTool

logger = logging.getLogger("webhook_agent.sequential_thinking")


def sequential_thinking(
    ctx: Context,
    thought: str,
    next_thought_needed: bool = True,
    thought_number: int = 1,
    total_thoughts: int = 3,
    is_revision: bool | None = None,
    revises_thought: int | None = None,
    branch_from_thought: int | None = None,
    branch_id: str | None = None,
) -> str:
    """Execute a step in a sequential thinking process for complex reasoning and analysis.

    Use this tool to:
    - Break down complex PR code audits into sequential steps.
    - Generate hypotheses and verify them against diffs and codebase searches.
    - Revise previous thoughts as understanding deepens.
    - Answer potential questions about codebase behavior BEFORE generating final review output.

    Args:
        ctx: ADK execution context.
        thought: Your current thinking step (analysis, hypothesis, revision, or verification).
        next_thought_needed: True if you need more thinking steps, False if analysis is complete.
        thought_number: Current thought step number (1, 2, 3...).
        total_thoughts: Estimated total thoughts needed.
        is_revision: True if this thought revises a previous thought step.
        revises_thought: The thought step number being revised (if is_revision is True).
        branch_from_thought: Thought number to branch from (if branching).
        branch_id: Optional identifier for a reasoning branch.

    Returns:
        Structured confirmation of the recorded thought step and current reasoning state.
    """
    if not thought or not thought.strip():
        return "Error: Empty thought provided."

    history: list[dict[str, Any]] = ctx.state.get("sequential_thoughts") or []

    step_record: dict[str, Any] = {
        "thought_number": thought_number,
        "total_thoughts": total_thoughts,
        "thought": thought.strip(),
        "next_thought_needed": next_thought_needed,
        "is_revision": bool(is_revision),
        "revises_thought": revises_thought,
        "branch_from_thought": branch_from_thought,
        "branch_id": branch_id,
    }

    history.append(step_record)
    ctx.state["sequential_thoughts"] = history

    logger.info(
        "Sequential thought recorded (%d/%d): %s...",
        thought_number,
        total_thoughts,
        thought[:60],
    )

    status = f"Recorded thought {thought_number}/{total_thoughts}."
    if is_revision and revises_thought:
        status += f" (Revises thought {revises_thought})"
    if next_thought_needed:
        status += " Next thought step requested."
    else:
        status += " Thinking complete. Proceed to synthesize final output."

    return status


sequential_thinking_tool = FunctionTool(sequential_thinking)
