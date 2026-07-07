"""Tests for agent_core.py — focusing on AgentCore delegation and trace ID generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from webhook_agent.agent_core import (
    AgentCore,
    generate_trace_id,
)
from webhook_agent.types import ActionResult


# ---------------------------------------------------------------------------
# Tests: generate_trace_id
# ---------------------------------------------------------------------------


class TestGenerateTraceId:
    def test_returns_hex_string(self):
        tid = generate_trace_id()
        assert isinstance(tid, str)
        assert len(tid) == 32  # 16 bytes = 32 hex chars
        assert all(c in "0123456789abcdef" for c in tid)

    def test_unique_ids(self):
        ids = {generate_trace_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Tests: AgentCore.run (integration with WebhookAgent)
# ---------------------------------------------------------------------------


class TestAgentCoreRun:
    def test_dry_run_returns_success(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-001",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        # Dry-run: WebhookAgent returns a single dry-run result
        assert len(results) == 1
        assert results[0].tool == "plan"
        assert results[0].success is True
        assert "dry-run" in results[0].detail

    def test_bot_sender_blocked_by_writeback_policy(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-002",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "hannibal-hub-agents[bot]"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is False
        assert "writeback policy" in results[0].detail

    def test_read_only_event_blocked(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-003",
            "event_name": "pull_request",
            "action": "synchronize",
            "canonical": "pull_request.synchronize",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "synchronize"},
        }
        results = core.run(ev, "owner/repo")
        # synchronize is read-only, WebhookAgent blocks it
        assert len(results) == 1
        assert results[0].success is False
        assert "read-only" in results[0].detail

    def test_mutations_disabled_by_policy(self):
        """When ALLOW_AUTOMATED_MUTATIONS is 0 and not dry-run, mutations are blocked."""
        core = AgentCore(gh_client=MagicMock(), dry_run=False)
        ev = {
            "delivery_id": "test-004",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is False
        assert "mutations are disabled" in results[0].detail

    def test_infer_canonical_from_event_data(self):
        """AgentCore still works when canonical is not explicitly set."""
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-005",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is True


# ---------------------------------------------------------------------------
# Tests: ActionResult
# ---------------------------------------------------------------------------


class TestActionResult:
    def test_create(self):
        r = ActionResult(tool="add_comment", success=True, detail="commented")
        assert r.tool == "add_comment"
        assert r.success is True
        assert r.detail == "commented"
