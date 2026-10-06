"""Tests for AgentCore delegation and trace ID generation (inlined into processor)."""

from unittest.mock import MagicMock

import pytest

from webhook_agent.processor import (
    AgentCore,
    generate_trace_id,
)
from webhook_agent.webhook_types import ActionResult

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

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
        r = ActionResult(tool="read_file", success=True, detail="read content")
        assert r.tool == "read_file"
        assert r.success is True
        assert r.detail == "read content"


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
        """get_model_chain should order models by capacity without duplicates and omit 3.6 on Free Tier."""
        from webhook_agent.webhook_agent import get_model_chain

        free_chain = get_model_chain()
        assert len(free_chain) == len(set(free_chain))
        assert "gemini-3.5-flash-lite" in free_chain
        assert "gemini-3.6-flash" not in free_chain
        assert "gemini-3.8-flash" not in free_chain
        assert "gemma-4-31b-it" in free_chain
        assert "gemma-4-26b-a4b-it" in free_chain

    def test_get_model_chain_paid_tier_orders_gemini_38_first(self, monkeypatch):
        """get_model_chain on Paid Tier should place gemini-3.8-flash as primary."""
        from webhook_agent.webhook_agent import get_model_chain

        monkeypatch.setenv("WEBHOOK_TIER", "paid")
        monkeypatch.delenv("GEMMA_MODEL", raising=False)
        paid_chain = get_model_chain()
        assert len(paid_chain) == len(set(paid_chain))
        assert paid_chain[0] == "gemini-3.8-flash"
        assert "gemini-3.7-flash" in paid_chain
        assert "gemini-3.6-flash" in paid_chain

    def test_primary_model_env_override(self, monkeypatch):
        """get_model_chain respects PRIMARY_MODEL environment variable."""
        from webhook_agent.webhook_agent import get_model_chain

        monkeypatch.setenv("PRIMARY_MODEL", "custom-model-override")
        chain = get_model_chain()
        assert chain[0] == "custom-model-override"

    def test_advance_model_chain_mutates_agent_model(self):
        """_advance_model_chain should dynamically cascade to the next tier model."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        initial_model = agent._current_model_name

        next_model = agent._advance_model_chain()
        assert next_model is not None
        assert agent._current_model_name == next_model
        assert agent._code_auditor.model.model == next_model
        assert agent._current_model_name != initial_model

    def test_advance_model_chain_does_not_recycle_depleted_models(self, monkeypatch):
        """A single agent run must never select a model that already failed."""
        from types import SimpleNamespace

        from webhook_agent import webhook_agent as module
        from webhook_agent.webhook_agent import WebhookAgent

        chain = [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        ]
        agent = WebhookAgent.__new__(WebhookAgent)
        agent._current_model_name = chain[0]
        agent._attempted_model_names = {chain[0]}
        agent._model_chain = chain
        agent._chain_index = 0
        agent._code_auditor = SimpleNamespace(model=None)
        agent._app = MagicMock()
        agent._session_service = MagicMock()
        agent._memory_service = MagicMock()

        monkeypatch.setattr(module, "get_model_chain", lambda: chain)
        monkeypatch.setattr(module, "_DEPLETED_MODEL_REGISTRY", MagicMock())
        monkeypatch.setattr(module, "get_adk_model", lambda **kwargs: MagicMock())
        monkeypatch.setattr(module, "Runner", MagicMock())
        selected = [agent._current_model_name]

        while next_model := agent._advance_model_chain(RuntimeError("500 INTERNAL")):
            selected.append(next_model)

        assert selected == chain
        assert selected.count("gemini-3.5-flash-lite") == 1


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
            "raw_payload": {"comment": {"body": "Hey @hannibal-hub-agents what do you think?"}},
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

    def test_truncate_text_preserves_full_text(self):
        from webhook_agent.webhook_agent import _truncate_text_to_token_limit

        long_text = "A" * 200000
        result = _truncate_text_to_token_limit(long_text, max_tokens=10, label="Test payload")
        assert result == long_text
        assert "truncated" not in result

    def test_build_user_message_preserves_pr_diff(self):
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
        assert "D" * 60000 in text

    def test_build_user_message_injects_deterministic_compiler_dossier(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        event_data = {
            "canonical": "pull_request.opened",
            "sender": {"login": "test-user"},
            "raw_payload": {
                "pull_request": {
                    "number": 1,
                    "title": "PR with Python code",
                },
                "pr_diff": "diff --git a/src/webhook_agent/tools/ast_tools.py b/src/webhook_agent/tools/ast_tools.py",
                "changed_files": ["src/webhook_agent/tools/ast_tools.py"],
            },
        }
        msg = agent._build_user_message(event_data)
        text = msg.parts[0].text
        assert "Deterministic Pre-Audit Compiler Findings" in text
        assert "AST Verification for 'src/webhook_agent/tools/ast_tools.py'" in text

    def test_build_user_message_injects_symbol_impact_dossier(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        event_data = {
            "canonical": "pull_request.opened",
            "sender": {"login": "test-user"},
            "raw_payload": {
                "pull_request": {
                    "number": 1,
                    "title": "PR modifying callable",
                },
                "pr_diff": "diff --git a/src/webhook_agent/core/github_tools.py b/src/webhook_agent/core/github_tools.py",
                "changed_files": ["src/webhook_agent/core/github_tools.py"],
            },
        }
        msg = agent._build_user_message(event_data)
        text = msg.parts[0].text
        assert "Deterministic Pre-Audit Compiler Findings" in text

    def test_code_auditor_preserves_review_context_and_disables_caching(self, monkeypatch):
        """The auditor retains PR context; enables caching for Gemini 3+ and disables for Gemma."""
        from webhook_agent.webhook_agent import WebhookAgent

        # Default Free Tier (gemini-3.5-flash-lite) enables caching with 4096 min tokens
        monkeypatch.setenv("WEBHOOK_TIER", "free")
        monkeypatch.delenv("PRIMARY_MODEL", raising=False)
        agent = WebhookAgent(dry_run=True)
        assert agent._code_auditor.include_contents == "default"
        assert "source of truth for this audit" in agent._code_auditor.instruction
        assert agent._app.context_cache_config is not None
        assert agent._app.context_cache_config.min_tokens == 4096

        # Gemma model must NEVER have context caching enabled
        monkeypatch.setenv("PRIMARY_MODEL", "gemma-4-31b-it")
        gemma_agent = WebhookAgent(dry_run=True)
        assert gemma_agent._app.context_cache_config is None


# ---------------------------------------------------------------------------
# Tests: Tool Registration (7 API-aligned primitives + utilities)
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_agent_tools_count(self):
        """Verify the exact tool count registered on the code auditor sub-agent.

        Forward-fix: review restored as interactive fallback alongside the 6
        deterministic grounding tools (Option A was too narrow for
        issue_comment reconciliation).
        """
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = [
            getattr(t, "name", getattr(t, "__name__", str(t))) for t in agent._code_auditor.tools
        ]
        assert len(tool_names) == 6

    def test_agent_tools_are_api_aligned(self):
        """Tool names should match the 5 audit-only tools plus review fallback."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = sorted(
            getattr(t, "name", getattr(t, "__name__", str(t))) for t in agent._code_auditor.tools
        )
        expected = sorted(
            [
                "read_file",
                "get_issue",
                "get_commit_diff",
                "get_current_time",
                "google_search_grounding_tool",
                "review",
            ]
        )
        assert tool_names == expected

    def test_no_removed_tools_present(self):
        """Removed and mutation tools should not be registered on the auditor."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", str(t))) for t in agent._code_auditor.tools
        }
        removed = {
            "verify_python_ast",
            "check_symbol_impact",
            "check_test_coverage",
            "get_pr_diff_file_map",
            "verify_line_reference",
            "add_label",
            "add_review_comment",
            "reply_to_review_comment",
            "submit_review",
            "assign_reviewers",
            "create_branch_commit",
            "get_pr_diff",
            "update_pr_description",
            "create_issue",
            # Pruned mutation tools
            "write_file",
            "update_issue",
            "add_comment",
            "open_pr",
            "update_branch_from_base",
            "resolve_pr_conflicts",
            "search_codebase",
            "auto_fix_pr_review_feedback",
            "mark_ready_for_review",
            "merge_pr",
            "sequential_thinking",
        }
        assert tool_names.isdisjoint(removed), f"Found removed tools: {tool_names & removed}"

    def test_code_auditor_bounded_thinking_budget(self, monkeypatch):
        """Auditor thinking budget must default to 1024 or respect AUDITOR_THINKING_BUDGET."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent_default = WebhookAgent(dry_run=True)
        assert agent_default._code_auditor.planner.thinking_config.thinking_budget == 1024

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "2048")
        agent_custom = WebhookAgent(dry_run=True)
        assert agent_custom._code_auditor.planner.thinking_config.thinking_budget == 2048

    def test_review_accepts_deterministic_precompiled_ast(self):
        """Deterministic precompiled AST satisfies review gate without runtime tool execution."""
        from unittest.mock import MagicMock

        from google.adk.tools import ToolContext

        from webhook_agent.core.github_tools import review

        mock_tc = MagicMock(spec=ToolContext)
        mock_file = MagicMock()
        mock_file.filename = "src/webhook_agent/core/test_module.py"
        mock_pr = MagicMock()
        mock_pr.get_files.return_value = [mock_file]
        mock_pr.get_reviews.return_value = []
        mock_gh = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_gh.get_repo.return_value = mock_repo

        mock_tc.state = {
            "gh_client": mock_gh,
            "repo_full_name": "owner/repo",
            "pr_number": 1,
            "deterministic_precompiled_ast": True,
            "tools_executed": [],
            "dry_run": True,
        }

        body = (
            '{"executive_summary": "All good", "critical_issues": [], '
            '"minor_suggestions": [], "risks_and_edge_cases": [], '
            '"verified_invariants": [{"invariant": "Precompiled AST integrity holds", '
            '"path": "src/webhook_agent/core/test_module.py", "line": 10, "evidence": "AST verified"}], '
            '"context_gaps": []}'
        )
        res = review(mock_tc, pr_number=1, event="APPROVE", body=body)
        assert "Review submission rejected" not in res

    def test_review_restored_on_auditor_for_comment_reconciliation(self):
        """Forward-fix: review tool must be present for issue_comment follow-ups."""
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", str(t))) for t in agent._code_auditor.tools
        }
        assert "review" in tool_names

    def test_issue_comment_on_pr_prefetches_diff(self):
        """Forward-fix: issue_comment on a PR gets diff context for grounding."""
        from unittest.mock import MagicMock

        from webhook_agent.processor import _should_prefetch_diff

        raw = {
            "comment": {"body": "looks good, thanks!"},
            "issue": {"number": 274, "pull_request": {"url": "x"}},
        }
        assert _should_prefetch_diff("issue_comment.created", raw) is True

    def test_issue_comment_on_issue_skips_diff(self):
        """Plain issue comments (no pull_request link) stay lightweight."""
        from webhook_agent.processor import _should_prefetch_diff

        raw = {
            "comment": {"body": "looks good, thanks!"},
            "issue": {"number": 10},
        }
        assert _should_prefetch_diff("issue_comment.created", raw) is False


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
# Tests: add_label tool
# ---------------------------------------------------------------------------


class TestAddLabel:
    def test_add_label_success(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import add_label

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        repo.get_issue.return_value = mock_issue

        result = add_label(ctx, 42, labels=["jules"])
        assert "Successfully added labels ['jules'] to #42" in result
        mock_issue.add_to_labels.assert_called_once_with("jules")

    def test_add_label_error_handling(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import add_label

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        repo.get_issue.side_effect = Exception("Not found")

        result = add_label(ctx, 42, labels=["jules"])
        assert "Error adding labels to #42" in result


# ---------------------------------------------------------------------------
# Tests: create_issue tool
# ---------------------------------------------------------------------------


class TestCreateIssue:
    def test_create_issue_success(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import create_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        mock_issue = MagicMock()
        mock_issue.number = 101
        mock_issue.html_url = "https://github.com/owner/repo/issues/101"
        repo.create_issue.return_value = mock_issue

        result = create_issue(
            ctx,
            title="refactor: clean up legacy code",
            body="Task specification for Jules",
            labels=["jules"],
        )
        assert "Successfully created issue #101" in result
        assert "https://github.com/owner/repo/issues/101" in result
        assert "labels ['jules']" in result
        repo.create_issue.assert_called_once_with(
            title="refactor: clean up legacy code",
            body="Task specification for Jules",
            labels=["jules"],
        )

    def test_create_issue_error_handling(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import create_issue

        ctx = MagicMock()
        ctx.state = {"gh_client": MagicMock(), "repo_full_name": "owner/repo"}

        repo = ctx.state["gh_client"].get_repo.return_value
        repo.create_issue.side_effect = Exception("API rate limit exceeded")

        result = create_issue(
            ctx,
            title="test issue",
            body="test body",
        )
        assert "Error creating issue: API rate limit exceeded" in result

    def test_conversational_agent_equipped_with_create_issue(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", str(t)))
            for t in agent._conversational_agent.tools
        }
        assert "create_issue" in tool_names
        assert "add_label" in tool_names


# Tests: get_max_input_tokens & payload truncation
# ---------------------------------------------------------------------------


class TestTokenLimits:
    def test_get_max_input_tokens_default_tier(self):
        from webhook_agent.webhook_agent import get_max_input_tokens

        assert get_max_input_tokens() == 3500


class TestGetCommitDiffBranchUpdate:
    def test_get_commit_diff_returns_branch_update_notice(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import get_commit_diff

        ctx = MagicMock()
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        ctx.state = {"gh_client": mock_gh, "repo_full_name": "owner/repo"}

        mock_commit = MagicMock()
        mock_commit.parents = [MagicMock(sha="parent1"), MagicMock(sha="parent2")]
        mock_commit.commit.message = "Merge branch 'main' into dependabot/uv/cryptography-50.0.1"
        mock_repo.get_commit.return_value = mock_commit

        res = get_commit_diff(ctx, "base_sha", "0abebcc123")

        assert "is a branch update merge commit" in res
        assert "No new PR-specific code changes were introduced." in res
        mock_repo.compare.assert_not_called()


class TestRpmWaiterTpmCeiling:
    def test_tpm_hard_ceiling_forces_wait(self, monkeypatch):
        import asyncio

        from webhook_agent.models.rate_limiter import RPMWaiter

        fake_now = 1000.0
        waiter = RPMWaiter(clock=lambda: fake_now)

        # Mock registry TPM limit to 100,000 for a test model
        waiter.model_limits = {"test-model": {"free": {"rpm": 10, "tpm": 100000, "rpd": 100.0}}}

        # Pre-seed token history with finalized tokens reaching 95,000 (95% > 90% threshold)
        norm_model = waiter._norm("test-model")
        waiter.token_histories[norm_model] = [[fake_now - 20.0, 95000, True]]

        # Intercept asyncio.sleep to check wait_time without actually sleeping
        slept_times = []

        async def mock_sleep(secs):
            slept_times.append(secs)

        monkeypatch.setattr("asyncio.sleep", mock_sleep)

        # Request even a tiny 100-token estimate; hard ceiling should trigger
        async def _run():
            await waiter.check_and_wait(
                model="test-model",
                estimated_tokens=100,
                tier="free",
            )

        asyncio.run(_run())

        assert len(slept_times) == 1
        # Expect wait for (fake_now - 20.0 + 60.0) - fake_now = 40.0s
        assert abs(slept_times[0] - 40.0) < 0.2


class TestReviewDismissalOrdering:
    def test_review_failure_does_not_dismiss_existing_reviews(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import review

        ctx = MagicMock()
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        ctx.state = {"gh_client": mock_gh, "repo_full_name": "owner/repo"}

        mock_existing = MagicMock()
        mock_existing.id = 101
        mock_existing.user.login = "hannibal-hub-agents[bot]"
        mock_existing.state = "CHANGES_REQUESTED"
        mock_pr.get_reviews.return_value = [mock_existing]
        mock_pr.state = "open"
        mock_pr.merged = False
        mock_pr.create_review.side_effect = RuntimeError("GitHub API 503")

        valid_body = (
            '{"executive_summary": "Approved code changes.", "confidence": 5, "critical_issues": [], '
            '"minor_suggestions": [], "risks_and_edge_cases": [], "verified_invariants": [{"invariant": "Locking order preserved", "path": "src/core.py", "line": 10, "evidence": "Tested"}], "context_gaps": []}'
        )
        res = review(ctx, pr_number=42, body=valid_body, event="APPROVE")
        assert "Error submitting review: GitHub API 503" in res
        mock_existing.dismiss.assert_not_called()

    def test_review_success_dismisses_prior_reviews_excluding_current(self):
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import review

        ctx = MagicMock()
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        ctx.state = {"gh_client": mock_gh, "repo_full_name": "owner/repo"}

        mock_prior = MagicMock()
        mock_prior.id = 101
        mock_prior.user.login = "hannibal-hub-agents[bot]"
        mock_prior.state = "CHANGES_REQUESTED"

        mock_new = MagicMock()
        mock_new.id = 202
        mock_new.user.login = "hannibal-hub-agents[bot]"
        mock_new.state = "APPROVED"
        mock_new.html_url = "https://github.com/owner/repo/pull/42#pullrequestreview-202"

        mock_pr.state = "open"
        mock_pr.merged = False
        mock_pr.create_review.return_value = mock_new
        mock_pr.get_reviews.return_value = [mock_prior, mock_new]

        valid_body = (
            '{"executive_summary": "Approved code changes.", "confidence": 5, "critical_issues": [], '
            '"minor_suggestions": [], "risks_and_edge_cases": [], "verified_invariants": [{"invariant": "Locking order preserved", "path": "src/core.py", "line": 10, "evidence": "Tested"}], "context_gaps": []}'
        )
        res = review(ctx, pr_number=42, body=valid_body, event="APPROVE")
        assert "Submitted review (APPROVE)" in res
        mock_prior.dismiss.assert_called_once_with(
            "Superseded by fresh code review on latest commit."
        )
        mock_new.dismiss.assert_not_called()


class TestReviewIdempotency:
    @staticmethod
    def _pr(head_sha="head-1", reviews=None):
        from unittest.mock import MagicMock

        pr = MagicMock()
        pr.state = "open"
        pr.merged = False
        pr.head.sha = head_sha
        pr.get_reviews.return_value = list(reviews or [])
        new_review = MagicMock()
        new_review.id = 202
        new_review.html_url = "https://github.com/owner/repo/pull/42#review-202"
        pr.create_review.return_value = new_review
        return pr

    @staticmethod
    def _bot_review(commit_id, state="APPROVED"):
        from unittest.mock import MagicMock

        review = MagicMock()
        review.id = 101
        review.user.login = "hannibal-hub-agents[bot]"
        review.commit_id = commit_id
        review.state = state
        return review

    def test_same_head_suppresses_duplicate(self):
        from webhook_agent.webhook_agent import _submit_formal_review

        prior = self._bot_review("head-1")
        pr = self._pr(reviews=[prior])

        result, submitted = _submit_formal_review(
            pr, "duplicate", "COMMENT", "owner/repo#100", {"review_mode": "initial"}
        )

        assert submitted is False
        assert "already exists for current head head-1" in result
        pr.create_review.assert_not_called()
        prior.dismiss.assert_not_called()

    def test_prior_head_renders_update_and_dismisses_after_success(self):
        from webhook_agent.webhook_agent import _submit_formal_review

        prior = self._bot_review("old-head")
        pr = self._pr(reviews=[prior])
        body = '{"executive_summary":"New commit reviewed.","critical_issues":[]}'

        result, submitted = _submit_formal_review(
            pr, body, "COMMENT", "owner/repo#101", {"review_mode": "sync"}
        )

        assert submitted is True
        assert "Submitted review (APPROVE)" in result
        submitted_body = pr.create_review.call_args.kwargs["body"]
        assert "## ⚡ Code Review Update: `APPROVE`" in submitted_body
        prior.dismiss.assert_called_once()

    def test_synchronize_initial_shaped_data_uses_update_renderer(self):
        from webhook_agent.webhook_agent import _enforce_verdict

        pr = self._pr(reviews=[self._bot_review("old-head")])
        body = '{"executive_summary":"Synchronize result.","critical_issues":[]}'

        rendered, verdict, _ = _enforce_verdict(body, "COMMENT", pr, review_mode="sync")

        assert verdict == "APPROVE"
        assert "## ⚡ Code Review Update: `APPROVE`" in rendered

    def test_concurrent_submissions_create_only_one_review(self):
        import threading
        import time
        from unittest.mock import MagicMock

        from webhook_agent.webhook_agent import _submit_formal_review

        pr = self._pr(head_sha="race-head")
        prior_reviews = []
        pr.get_reviews.side_effect = lambda: list(prior_reviews)
        created = MagicMock(id=303, html_url="https://example.test/review-303")

        def create_review(**_kwargs):
            time.sleep(0.03)
            prior_reviews.append(self._bot_review("race-head", state="APPROVED"))
            return created

        pr.create_review.side_effect = create_review
        results = []

        def submit():
            results.append(
                _submit_formal_review(
                    pr,
                    "race",
                    "COMMENT",
                    "owner/repo#102",
                    {"review_mode": "initial"},
                )
            )

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(submitted for _result, submitted in results) == 1
        assert pr.create_review.call_count == 1


class TestRunInBgLoop:
    def test_run_in_bg_loop_executes_coroutine(self):
        from webhook_agent.core.loop_helpers import run_in_bg_loop

        async def _sample_coro():
            return 42

        res = run_in_bg_loop(_sample_coro())
        assert res == 42

    def test_run_in_bg_loop_cancels_future_on_timeout(self):
        from concurrent.futures import TimeoutError as FutureTimeoutError
        from unittest.mock import MagicMock, patch

        from webhook_agent.core.loop_helpers import run_in_bg_loop

        async def _slow_coro():
            return "done"

        coro = _slow_coro()
        mock_future = MagicMock()
        mock_future.result.side_effect = FutureTimeoutError("Timed out")

        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
            with pytest.raises(FutureTimeoutError):
                run_in_bg_loop(coro)

        coro.close()
        # Verified that future.cancel() was called to prevent zombie task
        mock_future.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Conversational Agent and Review Intent Routing
# ---------------------------------------------------------------------------


class TestConversationalAgent:
    def test_review_intent_keywords_defined(self):
        from webhook_agent.review.writeback_policy import REVIEW_INTENT_KEYWORDS

        assert isinstance(REVIEW_INTENT_KEYWORDS, tuple)
        assert "/review" in REVIEW_INTENT_KEYWORDS
        assert "please review" in REVIEW_INTENT_KEYWORDS
        assert "re-review" in REVIEW_INTENT_KEYWORDS
        assert "audit this" in REVIEW_INTENT_KEYWORDS

    def test_is_formal_review_eligible(self):
        from webhook_agent.review.writeback_policy import _is_formal_review_eligible

        # PR lifecycle events are always review eligible
        assert _is_formal_review_eligible("pull_request.opened") is True
        assert _is_formal_review_eligible("pull_request.synchronize") is True
        assert _is_formal_review_eligible("pull_request.ready_for_review") is True
        assert _is_formal_review_eligible("pull_request.reopened") is True
        assert _is_formal_review_eligible("pull_request_review_requested") is True

        # Comments with review intent
        assert _is_formal_review_eligible("issue_comment.created", "Please /review this") is True
        assert (
            _is_formal_review_eligible(
                "issue_comment.created", "Can you please review the changes?"
            )
            is True
        )
        assert (
            _is_formal_review_eligible("pull_request_review_comment.created", "please re-review")
            is True
        )
        assert _is_formal_review_eligible("issue_comment.created", "audit this PR") is True
        assert _is_formal_review_eligible("issue_comment.created", "PLEASE REVIEW THIS PR") is True

        # Conversational chit-chat / routine comments are NOT review eligible
        assert (
            _is_formal_review_eligible(
                "issue_comment.created",
                "This is me testing the webhook auditor's conversational abilities btw :3",
            )
            is False
        )
        assert _is_formal_review_eligible("issue_comment.created", "Looks great, thanks!") is False
        assert (
            _is_formal_review_eligible("issue_comment.created", "Can we deploy this to staging?")
            is False
        )
        assert _is_formal_review_eligible("issues.opened", "Bug in parser") is False

    def test_conversational_agent_tools_and_instruction(self):
        from webhook_agent.webhook_agent import CONVERSATIONAL_INSTRUCTION, WebhookAgent

        agent = WebhookAgent(dry_run=True)
        assert hasattr(agent, "_conversational_agent")
        assert agent._conversational_agent.instruction == CONVERSATIONAL_INSTRUCTION
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", str(t)))
            for t in agent._conversational_agent.tools
        }
        # Tools should include codebase grounding tools but NOT review
        assert "read_file" in tool_names
        assert "search_codebase" not in tool_names
        assert "review" not in tool_names

    def test_conversational_comment_dispatch_posts_comment(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=False)
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_comment = MagicMock()
        mock_comment.html_url = "https://github.com/owner/repo/issues/42#issuecomment-999"
        mock_issue.create_comment.return_value = mock_comment

        # Clear rate limiter for test key
        _COMMENT_RATE_LIMITER._history.pop("owner/repo#42", None)

        fake_event = SimpleNamespace(
            usage_metadata=None,
            get_function_responses=list,
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        text="I'm doing well! The weather in CI is sunny :3",
                        thought=False,
                    )
                ]
            ),
        )

        async def fake_run_async(*args, **kwargs):
            yield fake_event

        monkeypatch.setattr(agent._conversational_runner, "run_async", fake_run_async)

        event_data = {
            "canonical": "issue_comment.created",
            "repo_name": "owner/repo",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "human-dev"},
            "raw_payload": {
                "repository": {"full_name": "owner/repo"},
                "issue": {
                    "number": 42,
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/42"},
                },
                "comment": {"body": "Hey auditor, how are you doing today? :3"},
            },
        }

        results = agent.plan_and_execute(event_data, mock_gh, trace_id="trace-test-conv")
        assert len(results) == 1
        assert results[0].tool == "add_comment"
        assert results[0].success is True
        mock_issue.create_comment.assert_called_once_with(
            "I'm doing well! The weather in CI is sunny :3"
        )

    def test_conversational_comment_rate_limited(self, monkeypatch):
        import time
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=False)
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value

        target_key = "owner/repo#43"
        now = time.time()
        _COMMENT_RATE_LIMITER._history[target_key] = [
            now - 10,
            now - 20,
            now - 30,
            now - 40,
            now - 50,
        ]

        fake_event = SimpleNamespace(
            usage_metadata=None,
            get_function_responses=list,
            content=SimpleNamespace(
                parts=[SimpleNamespace(text="Another quick reply", thought=False)]
            ),
        )

        async def fake_run_async(*args, **kwargs):
            yield fake_event

        monkeypatch.setattr(agent._conversational_runner, "run_async", fake_run_async)

        event_data = {
            "canonical": "issue_comment.created",
            "repo_name": "owner/repo",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "human-dev"},
            "raw_payload": {
                "repository": {"full_name": "owner/repo"},
                "issue": {
                    "number": 43,
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/43"},
                },
                "comment": {"body": "Spamming comments fast"},
            },
        }

        results = agent.plan_and_execute(event_data, mock_gh, trace_id="trace-test-rl")
        mock_issue.create_comment.assert_not_called()
        assert not any(r.tool == "add_comment" for r in results)

    def test_conversational_app_name_aligned_with_agent(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        assert agent._conversational_app.name == agent._app_name

    def test_build_user_message_for_issues_opened(self):
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=True)
        event_data = {
            "canonical": "issues.opened",
            "sender": {"login": "cgj8702"},
            "raw_payload": {
                "issue": {
                    "number": 233,
                    "title": "Testing conversational abilities",
                    "body": "Comment below your thoughts.",
                }
            },
        }
        msg = agent._build_user_message(event_data)
        text = msg.parts[0].text
        assert "Canonical Event: issues.opened" in text
        assert "Issue Number: 233" in text
        assert "Issue Title: Testing conversational abilities" in text
        assert "Issue Body: Comment below your thoughts." in text

    def test_issues_opened_conversational_dispatch(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER
        from webhook_agent.webhook_agent import WebhookAgent

        agent = WebhookAgent(dry_run=False)
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_comment = MagicMock()
        mock_comment.html_url = "https://github.com/owner/repo/issues/233#issuecomment-1"
        mock_issue.create_comment.return_value = mock_comment

        _COMMENT_RATE_LIMITER._history.pop("owner/repo#233", None)

        fake_event = SimpleNamespace(
            usage_metadata=None,
            get_function_responses=list,
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        text="Hello! I'm here and ready to pair program with you :3",
                        thought=False,
                    )
                ]
            ),
        )

        async def fake_run_async(*args, **kwargs):
            yield fake_event

        monkeypatch.setattr(agent._conversational_runner, "run_async", fake_run_async)

        event_data = {
            "canonical": "issues.opened",
            "repo_name": "owner/repo",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702"},
            "raw_payload": {
                "repository": {"full_name": "owner/repo"},
                "issue": {
                    "number": 233,
                    "title": "Testing conversational abilities",
                    "body": "Comment below your thoughts.",
                },
            },
        }

        results = agent.plan_and_execute(event_data, mock_gh, trace_id="trace-test-issue-open")
        assert len(results) == 1
        assert results[0].tool == "add_comment"
        assert results[0].success is True
        mock_issue.create_comment.assert_called_once_with(
            "Hello! I'm here and ready to pair program with you :3"
        )
