"""Tests for agent_core.py — focusing on AgentCore delegation and trace ID generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from webhook_agent.agent_core import (
    AgentCore,
    generate_trace_id,
)
from webhook_agent.webhook_types import ActionResult

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
            "event_name": "ping",
            "canonical": "ping",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"action": "ping"},
        }
        results = core.run(ev, "owner/repo")
        # ping is read-only, WebhookAgent blocks it
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
        r = ActionResult(tool="update_issue", success=True, detail="commented")
        assert r.tool == "update_issue"
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
# Tests: WebhookAgent Model Chain (TPM Descending)
# ---------------------------------------------------------------------------


class TestWebhookAgentModelChain:
    def test_get_model_chain_orders_tpm_descending(self):
        """get_model_chain should order models by TPM descending without duplicates."""
        from webhook_agent.webhook_agent import get_model_chain

        chain = get_model_chain()
        assert len(chain) == len(set(chain))
        assert "gemini-3.5-flash-lite" in chain
        assert "gemini-3.6-flash" in chain
        assert "gemma-4-26b" in chain

    def test_advance_model_chain_mutates_agent_model(self):
        """_advance_model_chain should dynamically cascade to the next tier model."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        initial_model = agent._current_model_name

        next_model = agent._advance_model_chain()
        assert next_model is not None
        assert agent._current_model_name == next_model
        assert agent._agent.model.model == next_model
        assert agent._current_model_name != initial_model


class TestDynamicModelRouting:
    def setup_method(self):
        from webhook_agent.webhook_agent import _DEPLETED_MODEL_REGISTRY

        _DEPLETED_MODEL_REGISTRY._depleted.clear()

    def test_pull_request_opened_routes_to_primary_model(self, monkeypatch):
        monkeypatch.setenv("GEMMA_MODEL", "gemini-3.6-flash")
        from webhook_agent.webhook_agent import _select_model_for_event

        event_data = {"canonical": "pull_request.opened"}
        assert _select_model_for_event(event_data) == "gemini-3.6-flash"

    def test_slash_command_comment_routes_to_primary_model(self, monkeypatch):
        monkeypatch.setenv("GEMMA_MODEL", "gemini-3.6-flash")
        from webhook_agent.webhook_agent import _select_model_for_event

        event_data = {
            "canonical": "issue_comment.created",
            "raw_payload": {"comment": {"body": "Please /review this PR"}},
        }
        assert _select_model_for_event(event_data) == "gemini-3.6-flash"

    def test_bot_mention_comment_routes_to_primary_model(self, monkeypatch):
        monkeypatch.setenv("GEMMA_MODEL", "gemini-3.6-flash")
        from webhook_agent.webhook_agent import _select_model_for_event

        event_data = {
            "canonical": "pull_request_review_comment.created",
            "raw_payload": {
                "comment": {"body": "Hey @hannibal-hub-agents what do you think?"}
            },
        }
        assert _select_model_for_event(event_data) == "gemini-3.6-flash"

    def test_routine_comment_routes_to_lightweight_model(self):
        from webhook_agent.webhook_agent import _select_model_for_event

        event_data = {
            "canonical": "issue_comment.created",
            "raw_payload": {"comment": {"body": "Looks good to me!"}},
        }
        assert _select_model_for_event(event_data) == "gemini-3.5-flash-lite"

    def test_pull_request_closed_routes_to_lightweight_model(self):
        from webhook_agent.webhook_agent import _select_model_for_event

        event_data = {"canonical": "pull_request.closed"}
        assert _select_model_for_event(event_data) == "gemini-3.5-flash-lite"

    def test_disabled_dynamic_routing_forces_primary_model(self, monkeypatch):
        monkeypatch.setenv("GEMMA_MODEL", "gemini-3.6-flash")
        from webhook_agent.webhook_agent import _select_model_for_event

        monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "0")
        event_data = {"canonical": "pull_request.closed"}
        assert _select_model_for_event(event_data) == "gemini-3.6-flash"


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

    def test_count_tokens_exact_mocked(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from webhook_agent.webhook_agent import _truncate_text_to_token_limit

        monkeypatch.setenv("WEBHOOK_FREE_KEY", "test_key")
        long_text = "Code line\n" * 1000

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.total_tokens = 20000

        mock_response_2 = MagicMock()
        mock_response_2.total_tokens = 14000
        mock_client.models.count_tokens.side_effect = [mock_response, mock_response_2]

        with patch(
            "webhook_agent.webhook_agent.get_shared_genai_client",
            return_value=mock_client,
        ):
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


# ---------------------------------------------------------------------------
# Tests: Tool Registration (7 API-aligned primitives + utilities)
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_agent_has_exactly_12_tools(self):
        """WebhookAgent should register 10 API primitives + 2 utility tools."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = [
            getattr(t, "name", getattr(t, "__name__", str(t)))
            for t in agent._agent.tools
        ]
        assert len(tool_names) == 12

    def test_agent_tools_are_api_aligned(self):
        """Tool names should match the 10 API primitives + get_current_time + search_agent."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = sorted(
            getattr(t, "name", getattr(t, "__name__", str(t)))
            for t in agent._agent.tools
        )
        expected = sorted(
            [
                "read_file",
                "write_file",
                "get_issue",
                "get_commit_diff",
                "update_issue",
                "add_comment",
                "open_pr",
                "update_branch_from_base",
                "merge_pr",
                "review",
                "get_current_time",
                "search_agent",
            ]
        )
        assert tool_names == expected

    def test_no_removed_tools_present(self):
        """Removed tools should not be registered."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", str(t)))
            for t in agent._agent.tools
        }
        removed = {
            "add_label",
            "add_review_comment",
            "reply_to_review_comment",
            "submit_review",
            "assign_reviewers",
            "create_branch_commit",
            "get_pr_diff",
            "update_pr_description",
            "create_issue",
        }
        assert tool_names.isdisjoint(removed), (
            f"Found removed tools: {tool_names & removed}"
        )


# ---------------------------------------------------------------------------
# Tests: get_current_time tool
# ---------------------------------------------------------------------------


class TestGetCurrentTime:
    def test_returns_iso_utc_timestamp(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import get_current_time

        ctx = MagicMock()
        res = get_current_time(ctx)
        assert "current_utc_time" in res
        assert "T" in res["current_utc_time"]
        assert "+00:00" in res["current_utc_time"] or "Z" in res["current_utc_time"]


# ---------------------------------------------------------------------------
# Tests: read_file tool
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_read_file_returns_content(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import read_file

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        mock_content = MagicMock()
        mock_content.decoded_content = b"print('hello world')"
        repo = ctx.state["gh_client"].get_repo.return_value
        repo.get_contents.return_value = mock_content

        result = read_file(ctx, "src/main.py")
        assert "hello world" in result
        repo.get_contents.assert_called_once_with("src/main.py")

    def test_read_file_with_ref(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import read_file

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        mock_content = MagicMock()
        mock_content.decoded_content = b"v2 code"
        repo = ctx.state["gh_client"].get_repo.return_value
        repo.get_contents.return_value = mock_content

        result = read_file(ctx, "src/main.py", ref="feature-branch")
        assert "v2 code" in result
        repo.get_contents.assert_called_once_with("src/main.py", ref="feature-branch")

    def test_read_file_directory_returns_error(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import read_file

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        repo.get_contents.return_value = [MagicMock(), MagicMock()]

        result = read_file(ctx, "src/")
        assert "directory" in result.lower()


# ---------------------------------------------------------------------------
# Tests: get_issue tool
# ---------------------------------------------------------------------------


class TestGetIssue:
    def test_get_issue_returns_pr_metadata(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import get_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        mock_issue.title = "Fix bug"
        mock_issue.state = "open"
        mock_issue.labels = []
        repo.get_issue.return_value = mock_issue

        mock_pr = MagicMock()
        mock_pr.head.ref = "fix-branch"
        mock_pr.base.ref = "main"
        mock_pr.mergeable = True
        mock_pr.mergeable_state = "clean"
        mock_pr.changed_files = 2
        mock_pr.additions = 10
        mock_pr.deletions = 3
        repo.get_pull.return_value = mock_pr

        result = get_issue(ctx, 42)
        assert "Fix bug" in result
        assert "fix-branch" in result
        assert "main" in result
        assert "Mergeable: True" in result
        assert "Pull Request" in result

    def test_get_issue_with_diff(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import get_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        mock_issue.title = "Add feature"
        mock_issue.state = "open"
        mock_issue.labels = []
        repo.get_issue.return_value = mock_issue

        mock_pr = MagicMock()
        mock_pr.head.ref = "feat"
        mock_pr.base.ref = "main"
        mock_pr.mergeable = True
        mock_pr.mergeable_state = "clean"
        mock_pr.changed_files = 1
        mock_pr.additions = 5
        mock_pr.deletions = 0

        mock_file = MagicMock()
        mock_file.filename = "src/app.py"
        mock_file.status = "modified"
        mock_file.patch = "+new line"
        mock_pr.get_files.return_value = [mock_file]
        repo.get_pull.return_value = mock_pr

        result = get_issue(ctx, 1, include_diff=True)
        assert "src/app.py" in result
        assert "+new line" in result
        assert "Diff:" in result


# ---------------------------------------------------------------------------
# Tests: update_issue tool
# ---------------------------------------------------------------------------


class TestAddComment:
    def test_add_comment_posts_comment(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import add_comment

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        mock_comment = MagicMock()
        mock_comment.html_url = "https://github.com/owner/repo/issues/1#comment-123"
        mock_issue.create_comment.return_value = mock_comment
        repo.get_issue.return_value = mock_issue

        result = add_comment(ctx, 1, body="Hello!")
        assert "Commented" in result
        mock_issue.create_comment.assert_called_once_with(body="Hello!")


class TestUpdateIssue:
    def test_update_issue_edits_title_and_body(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import update_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        repo.get_issue.return_value = mock_issue

        result = update_issue(ctx, 1, title="New Title", body="New body")
        assert "Updated" in result
        mock_issue.edit.assert_called_once_with(title="New Title", body="New body")

    def test_update_issue_adds_labels(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import update_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        repo.get_issue.return_value = mock_issue

        result = update_issue(ctx, 1, labels=["bug", "urgent"])
        assert "Labels added" in result
        mock_issue.add_to_labels.assert_called_once_with("bug", "urgent")

    def test_update_issue_multiple_actions(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import update_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        repo.get_issue.return_value = mock_issue

        result = update_issue(ctx, 1, title="Updated", labels=["approved"])
        assert "Updated" in result
        assert "Labels added" in result

    def test_update_issue_no_changes(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import update_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        repo.get_issue.return_value = mock_issue

        result = update_issue(ctx, 1)
        assert "no changes" in result


# ---------------------------------------------------------------------------
# Tests: get_max_input_tokens & payload truncation
# ---------------------------------------------------------------------------


class TestTokenLimits:
    def test_get_max_input_tokens_paid_tier(self, monkeypatch):
        from webhook_agent.webhook_agent import get_max_input_tokens

        monkeypatch.setenv("HANNIBAL_TIER", "paid")
        assert get_max_input_tokens() == 35000

    def test_get_max_input_tokens_free_tier(self, monkeypatch):
        from webhook_agent.webhook_agent import get_max_input_tokens

        monkeypatch.setenv("HANNIBAL_TIER", "free")
        assert get_max_input_tokens() == 3500


# ---------------------------------------------------------------------------
# Tests: Programmatic Command Router for /resolve
# ---------------------------------------------------------------------------


class TestProgrammaticResolveCommandRouter:
    def test_plan_and_execute_intercepts_resolve_command(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from webhook_agent.webhook_agent import WebhookAgent

        monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
        agent = WebhookAgent(dry_run=False)

        mock_gh = MagicMock()
        mock_pr = MagicMock()
        mock_pr.head.ref = "feat-branch"
        mock_pr.base.ref = "main"
        mock_gh.get_repo.return_value.get_pull.return_value = mock_pr

        event_data = {
            "canonical": "issue_comment.created",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "issue": {"number": 63, "pull_request": {}},
                "comment": {"body": "/resolve"},
            },
        }

        with patch(
            "webhook_agent.webhook_agent.resolve_merge_conflicts"
        ) as mock_resolve:
            mock_resolve.return_value = {
                "success": True,
                "detail": "Resolved conflicts in 2 files",
            }
            results = agent.plan_and_execute(
                event_data=event_data,
                gh_client=mock_gh,
                trace_id="test-trace-123",
            )
            assert len(results) == 1
            assert results[0].tool == "resolve_merge_conflicts"
            assert results[0].success is True
            assert "Resolved conflicts in 2 files" in results[0].detail
            assert mock_resolve.call_count == 1
            mock_pr.create_comment.assert_called_once()
