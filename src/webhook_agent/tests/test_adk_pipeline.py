"""Unit tests for ADK sub-agent state pipeline and deterministic review submission."""

from __future__ import annotations

import json

from webhook_agent.formatter import CodeReviewResponse
from webhook_agent.state_graph import ADKStateGraph, GraphState
from webhook_agent.webhook_agent import _enforce_verdict


class TestADKPipeline:
    def test_state_graph_preflight(self):
        graph = ADKStateGraph()
        state = graph.evaluate_preflight(
            canonical="pull_request.opened",
            repo_name="cgj8702-org/hannibal-hub",
            raw_payload={"pull_request": {"number": 42}, "is_stale_thread": True},
        )
        assert isinstance(state, GraphState)
        assert state.pr_number == 42
        assert "stale_thread_reminder" in state.proactive_actions

    def test_deterministic_payload_serialization_pydantic(self):
        cr = CodeReviewResponse(
            executive_summary="All tests pass cleanly.",
            confidence=5,
            critical_issues=[],
            minor_suggestions=[],
            risks_and_edge_cases=[],
        )
        raw_json = cr.model_dump_json()
        body, event, _comments = _enforce_verdict(raw_json, "COMMENT")
        assert event == "APPROVE"
        assert "All tests pass cleanly." in body

    def test_deterministic_payload_serialization_dict(self):
        payload_dict = {
            "executive_summary": "Detected critical syntax error in worker.",
            "confidence": 5,
            "critical_issues": ["Line 42 has syntax error"],
            "minor_suggestions": [],
            "risks_and_edge_cases": [],
        }
        body, event, _comments = _enforce_verdict(json.dumps(payload_dict), "COMMENT")
        assert event == "REQUEST_CHANGES"
        assert "Line 42 has syntax error" in body
