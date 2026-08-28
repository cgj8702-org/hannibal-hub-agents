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

from .webhook_agent import WebhookAgent
from .webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.core")


def generate_trace_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Agent Core
# ---------------------------------------------------------------------------
class AgentCore:
    """Entry point for webhook event processing.

    Delegates planning and execution to the ADK-powered WebhookAgent.
    """

    def __init__(self, gh_client=None, dry_run: bool = False, planner=None):
        # gh_client is optional so a long-lived AgentCore can be constructed
        # once and reused while callers supply a fresh GitHub client per-call.
        self.gh = gh_client
        self.dry_run = dry_run
        # WebhookAgent replaces GemmaPlanner entirely
        self._webhook_agent = WebhookAgent(dry_run=dry_run)

    def run(
        self,
        event_data: dict[str, Any],
        repo_full_name: str,
        trace_id: str | None = None,
        gh_client=None,
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

        # Use the per-call gh_client when provided, otherwise fall back to
        # the client stored on the instance. This allows a single long-lived
        # AgentCore to be reused while callers refresh installation tokens.
        gh = gh_client if gh_client is not None else self.gh

        # Delegate to the ADK-powered webhook agent
        results = self._webhook_agent.plan_and_execute(
            event_data=event_data,
            gh_client=gh,
            trace_id=trace_id,
        )

        logger.debug(
            "🧠 ADK agent processing completed (trace: %s, result_count: %d)",
            trace_id[-4:],
            len(results) if results else 0,
        )

        return results
