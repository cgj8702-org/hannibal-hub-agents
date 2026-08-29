"""ADK State Graph Engine for Webhook Agent Execution Pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from webhook_agent.formatter import (
    CodeReviewResponse,
    calculate_strict_verdict,
    normalize_code_review_dict,
)

logger = logging.getLogger("webhook_agent.graph")


@dataclass
class GraphState:
    """State context passed across ADK State Graph Nodes."""

    canonical: str = ""
    repo_name: str = ""
    pr_number: int | None = None
    issue_number: int | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    scope: str = "core_backend"
    context: dict[str, Any] = field(default_factory=dict)
    proactive_actions: list[str] = field(default_factory=list)
    audit_dict: dict[str, Any] = field(default_factory=dict)
    response: CodeReviewResponse | None = None
    verdict: str = "COMMENT"


class ScopeRouterNode:
    """Node 1: Classifies event scope (e.g. core_backend, feature_agent, docs)."""

    def process(self, state: GraphState) -> GraphState:
        raw = state.raw_payload
        if "pull_request" in raw:
            state.pr_number = raw["pull_request"].get("number")
        elif "issue" in raw:
            state.issue_number = raw["issue"].get("number")
            if raw["issue"].get("pull_request"):
                state.pr_number = state.issue_number

        state.scope = "core_backend"
        logger.debug(
            "Graph Node 1 [ScopeRouter]: classified scope=%s (PR #%s)",
            state.scope,
            state.pr_number,
        )
        return state


class ContextHydrationNode:
    """Node 2: Hydrates diffs, commit history, and prior review context."""

    def process(self, state: GraphState) -> GraphState:
        raw = state.raw_payload
        state.context["pr_diff"] = raw.get("pr_diff", "")
        state.context["commit_history"] = raw.get("commit_history_summary", "")
        state.context["previous_bot_reviews"] = raw.get("previous_bot_reviews", "")
        logger.debug(
            "Graph Node 2 [ContextHydration]: hydrated diff_len=%d",
            len(state.context["pr_diff"]),
        )
        return state


class StateEvaluatorNode:
    """Node 3: Evaluates proactive state rules and pre-execution conditions."""

    def process(self, state: GraphState) -> GraphState:
        if state.raw_payload.get("is_stale_thread"):
            state.proactive_actions.append("stale_thread_reminder")
        if state.raw_payload.get("has_merge_conflict"):
            state.proactive_actions.append("merge_conflict_warning")
        logger.debug(
            "Graph Node 3 [StateEvaluator]: proactive_actions=%s",
            state.proactive_actions,
        )
        return state


class CodeAuditorNode:
    """Node 4: Prepares raw LLM audit payload structure."""

    def process(
        self, state: GraphState, llm_response_dict: dict[str, Any]
    ) -> GraphState:
        state.audit_dict = llm_response_dict
        logger.debug("Graph Node 4 [CodeAuditor]: audit payload ingested")
        return state


class VerdictNormalizerNode:
    """Node 5: Normalizes CodeReviewResponse and calculates strict verdict safety."""

    def process(self, state: GraphState) -> GraphState:
        normalized = normalize_code_review_dict(state.audit_dict)
        cr_obj = CodeReviewResponse.model_validate(normalized)
        strict_verdict = calculate_strict_verdict(cr_obj)
        state.verdict = strict_verdict
        state.response = cr_obj
        logger.info(
            "Graph Node 5 [VerdictNormalizer]: final verdict=%s for PR #%s",
            state.verdict,
            state.pr_number,
        )
        return state


class ADKStateGraph:
    """Orchestrates 5-node State Graph workflow execution."""

    def __init__(self) -> None:
        self.router = ScopeRouterNode()
        self.hydration = ContextHydrationNode()
        self.evaluator = StateEvaluatorNode()
        self.auditor = CodeAuditorNode()
        self.normalizer = VerdictNormalizerNode()

    def run(
        self,
        canonical: str,
        repo_name: str,
        raw_payload: dict[str, Any],
        audit_dict: dict[str, Any] | None = None,
    ) -> GraphState:
        state = GraphState(
            canonical=canonical,
            repo_name=repo_name,
            raw_payload=raw_payload,
        )
        state = self.router.process(state)
        state = self.hydration.process(state)
        state = self.evaluator.process(state)
        if audit_dict:
            state = self.auditor.process(state, audit_dict)
            state = self.normalizer.process(state)
        return state
