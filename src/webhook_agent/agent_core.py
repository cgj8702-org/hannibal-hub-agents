"""Agent core — entry point for webhook event processing.

This module provides the top-level interface between the webhook processor
and the ADK-powered webhook agent. It handles:
- Canonical event construction
- Trace ID propagation
- Delegation to WebhookAgent for planning and execution
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .types import ActionResult
from .webhook_agent import WebhookAgent

logger = logging.getLogger("agent_core")


def generate_trace_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Agent Core
# ---------------------------------------------------------------------------
class AgentCore:
    """Entry point for webhook event processing.

    Delegates planning and execution to the ADK-powered WebhookAgent.
    """

    def __init__(self, gh_client, dry_run: bool = False, planner=None):
        self.gh = gh_client
        self.dry_run = dry_run
        # WebhookAgent replaces GemmaPlanner entirely
        self._webhook_agent = WebhookAgent(dry_run=dry_run)

    def run(
        self,
        event_data: dict[str, Any],
        repo_full_name: str,
        trace_id: str | None = None,
    ) -> list[ActionResult]:
        """Process a normalized event through the ADK-powered agent.

        Delegates all planning and execution to WebhookAgent.
        """
        trace_id = trace_id or generate_trace_id()

        logger.debug(
            "🧠 Starting ADK agent processing (trace: %s, repo: %s)",
            trace_id[-4:],
            repo_full_name,
        )

        logger.info(
            "🧠 Processing event via ADK agent (trace: %s, repo: %s)",
            trace_id[-4:],
            repo_full_name,
        )

        # Delegate to the ADK-powered webhook agent
        results = self._webhook_agent.plan_and_execute(
            event_data=event_data,
            gh_client=self.gh,
            trace_id=trace_id,
        )

        logger.debug(
            "🧠 ADK agent processing completed (trace: %s, result_count: %d)",
            trace_id[-4:],
            len(results) if results else 0,
        )

        return results
