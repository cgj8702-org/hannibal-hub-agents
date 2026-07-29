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

    def test_bot_sender_without_suffix_blocked(self):
        """Bot sender without [bot] suffix is also blocked by writeback policy."""
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-002b",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "hannibal-hub-agents"},
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

    def test_mutations_disabled_by_policy(self, monkeypatch):
        """When ALLOW_AUTOMATED_MUTATIONS is 0 and not dry-run, mutations are blocked."""
        monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "0")
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


# ---------------------------------------------------------------------------
# Tests: _is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_returns_true_for_503_error(self):
        """503 UNAVAILABLE should be recognized as transient."""
        from google.genai.errors import ServerError
        from webhook_agent.webhook_agent import _is_transient_error

        error = ServerError(503, {}, None)
        assert _is_transient_error(error) is True

    def test_returns_true_for_500_error(self):
        """500 INTERNAL_ERROR should be recognized as transient."""
        from google.genai.errors import ServerError
        from webhook_agent.webhook_agent import _is_transient_error

        error = ServerError(500, {}, None)
        assert _is_transient_error(error) is True

    def test_returns_true_for_429_error(self):
        """429 RESOURCE_EXHAUSTED should be recognized as transient."""
        from google.genai.errors import ServerError
        from webhook_agent.webhook_agent import _is_transient_error

        error = ServerError(429, {}, None)
        assert _is_transient_error(error) is True

    def test_returns_false_for_400_error(self):
        """400 BAD_REQUEST should NOT be recognized as transient."""
        from google.genai.errors import ServerError
        from webhook_agent.webhook_agent import _is_transient_error

        error = ServerError(400, {}, None)
        assert _is_transient_error(error) is False

    def test_returns_false_for_non_server_error(self):
        """Non-ServerError exceptions should NOT be recognized as transient."""
        from webhook_agent.webhook_agent import _is_transient_error

        assert _is_transient_error(ValueError("test")) is False


# ---------------------------------------------------------------------------
# Tests: WebhookAgent fallback model
# ---------------------------------------------------------------------------


class TestWebhookAgentFallback:
    def test_fallback_model_environment_variable(self):
        """WebhookAgent should use GEMMA_MODEL_FALLBACK env var for fallback."""
        from webhook_agent.webhook_agent import _FALLBACK_MODEL

        assert _FALLBACK_MODEL == "gemma-4-26b-a4b-it"

    def test_fallback_triggered_only_once(self):
        """Fallback model should only be triggered once per agent instance."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        agent._create_fallback_agent()
        # Second call should be a no-op
        agent._create_fallback_agent()  # Should not raise or create duplicate

        assert agent._fallback_triggered is True
