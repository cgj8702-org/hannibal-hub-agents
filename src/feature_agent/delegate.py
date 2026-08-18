"""Sub-agent delegation, HITL resurfacing, and child-to-parent escalation tools.

Implements the long-horizon-harness delegate pattern with isolated context windows,
ChildPending confirmation channels, and ask_parent decision tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.context import Context
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from feature_agent.agent import build_feature_app

logger = logging.getLogger("feature_agent.delegate")

DELEGATE_APP_NAME = "feature_delegate_child"
DELEGATE_USER_ID = "feature_delegate_caller"


@dataclass(frozen=True)
class ChildPending:
    """The child agent paused on a risky operation; surface to parent/user for confirmation."""

    confirmation_id: str
    hint: str
    payload: Any


@dataclass
class ChildDriveResult:
    """Result envelope from running a delegated child sub-agent."""

    status: str  # "completed" | "timeout" | "halted" | "pending"
    summary: str
    telemetry: dict[str, Any] = field(default_factory=dict)
    pending: ChildPending | None = None


def ask_parent(ctx: Context, question: str) -> str:
    """Escalate a clarifying architectural decision question from a child agent to the parent/user."""
    logger.info("🙋 Child Agent asked parent/user: %s", question)
    raw = getattr(ctx, "state", None)
    if isinstance(raw, dict):
        raw["last_parent_question"] = question
    return f"Question escalated to parent/user: {question}"


def _pending_from_event(event: Any) -> ChildPending | None:
    for fc in getattr(event, "get_function_calls", lambda: [])() or []:
        if getattr(fc, "name", None) == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
            tc = (getattr(fc, "args", {}) or {}).get("toolConfirmation", {}) or {}
            return ChildPending(
                confirmation_id=getattr(fc, "id", "") or "",
                hint=str(tc.get("hint", "")),
                payload=tc.get("payload"),
            )
    return None


def _event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(part.text for part in parts if getattr(part, "text", None))


class _MaxIterationsExceeded(Exception):
    """Sentinel for short-circuiting runaway child loops."""


async def drive_child(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    new_message: Content,
    timeout_s: float = 120.0,
    max_iterations: int | None = None,
) -> ChildDriveResult:
    """Drain one child runner pass, capturing output text and detecting HITL pending approvals."""
    chunks: list[str] = []
    iterations = 0
    pending: ChildPending | None = None
    status = "completed"
    summary_override: str | None = None
    start = time.monotonic()

    async def _drain() -> None:
        nonlocal iterations, pending
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=new_message
        ):
            iterations += 1
            text = _event_text(event)
            if text:
                chunks.append(text)
            found = _pending_from_event(event)
            if found is not None:
                pending = found
            if max_iterations is not None and iterations >= max_iterations:
                raise _MaxIterationsExceeded()

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    except TimeoutError:
        status = "timeout"
    except _MaxIterationsExceeded:
        status = "halted"
        summary_override = f"max_iterations ({max_iterations}) exceeded — child halted."
    except Exception as exc:
        logger.exception("Delegate child raised %s", type(exc).__name__)
        status = "halted"
        summary_override = f"Child crashed with {type(exc).__name__}: {exc}"

    if pending is not None:
        status = "pending"

    duration_ms = int((time.monotonic() - start) * 1000)
    return ChildDriveResult(
        status=status,
        summary=summary_override if summary_override is not None else "".join(chunks),
        telemetry={
            "iterations": iterations,
            "duration_ms": duration_ms,
        },
        pending=pending,
    )


async def run_child_delegate(
    goal: str,
    context_info: str = "",
    timeout_s: float = 120.0,
) -> ChildDriveResult:
    """Spawn a isolated child sub-agent runner to execute a delegated sub-task."""
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()

    runner = Runner(
        app=build_feature_app(),
        session_service=session_service,
        memory_service=memory_service,
    )

    session = await session_service.create_session(
        app_name=DELEGATE_APP_NAME, user_id=DELEGATE_USER_ID
    )

    prompt = f"Goal: {goal}\nContext: {context_info}"
    msg = Content(role="user", parts=[Part(text=prompt)])

    return await drive_child(
        runner=runner,
        user_id=DELEGATE_USER_ID,
        session_id=session.id,
        new_message=msg,
        timeout_s=timeout_s,
    )
