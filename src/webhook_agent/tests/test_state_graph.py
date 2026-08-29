"""Unit tests for ADKStateGraph pipeline."""

from __future__ import annotations

from webhook_agent.state_graph import ADKStateGraph, GraphState


class TestADKStateGraph:
    def test_state_graph_node_execution(self):
        graph = ADKStateGraph()
        raw_payload = {
            "pull_request": {"number": 112},
            "pr_diff": "diff --git a/foo.py b/foo.py",
            "commit_history_summary": "- `abc1234` (user): feat",
            "is_stale_thread": True,
        }
        audit_dict = {
            "executive_summary": "Clean code changes.",
            "confidence": 5,
            "critical_issues": [],
            "minor_suggestions": [],
            "risks_and_edge_cases": [],
        }

        state = graph.run(
            canonical="pull_request.opened",
            repo_name="owner/repo",
            raw_payload=raw_payload,
            audit_dict=audit_dict,
        )

        assert isinstance(state, GraphState)
        assert state.pr_number == 112
        assert state.scope == "core_backend"
        assert state.context["pr_diff"] == "diff --git a/foo.py b/foo.py"
        assert "stale_thread_reminder" in state.proactive_actions
        assert state.response is not None
        assert state.verdict == "APPROVE"
        assert state.response.confidence == 5
