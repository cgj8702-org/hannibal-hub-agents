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


# ---------------------------------------------------------------------------
# Tests: Input Token Safety Truncation
# ---------------------------------------------------------------------------


class TestTokenTruncation:
    def test_truncate_text_under_limit_unchanged(self):
        from webhook_agent.webhook_agent import _truncate_text_to_token_limit

        short_text = "Hello world"
        assert _truncate_text_to_token_limit(short_text, max_tokens=100) == short_text

    def test_truncate_text_over_limit_fallback_appends_warning(self):
        from webhook_agent.webhook_agent import _truncate_text_to_token_limit

        long_text = "A" * 200
        truncated = _truncate_text_to_token_limit(
            long_text, max_tokens=10, label="Test payload"
        )
        assert "truncated" in truncated
        assert "to stay within 10 token limit" in truncated

    def test_count_tokens_exact_mocked(self):
        from unittest.mock import MagicMock, patch

        from webhook_agent.webhook_agent import _truncate_text_to_token_limit

        long_text = "Code line\n" * 1000

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.total_tokens = 20000

        mock_response_2 = MagicMock()
        mock_response_2.total_tokens = 14000
        mock_client.models.count_tokens.side_effect = [mock_response, mock_response_2]

        with patch("google.genai.Client", return_value=mock_client):
            truncated = _truncate_text_to_token_limit(
                long_text, max_tokens=15000, label="Exact limit test"
            )
            assert "reduced to 14000 tokens" in truncated

    def test_build_user_message_truncates_large_pr_diff(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        event_data = {
            "canonical": "pull_request.opened",
            "sender": {"login": "test-user"},
            "raw_payload": {
                "pull_request": {
                    "number": 1,
                    "title": "Huge PR",
                },
                "pr_diff": "D" * 60000,
            },
        }
        msg = agent._build_user_message(event_data)
        text = msg.parts[0].text
        assert len(text) < 60000  # Truncated
        assert "token limit" in text
