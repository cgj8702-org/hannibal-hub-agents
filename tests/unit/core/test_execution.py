"""Unit tests for core.execution execution runner and writeback dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from webhook_agent.core.execution import (
    _execute_adk_runner,
    _post_conversational_reply,
    _submit_deterministic_review,
    execute_agent_event,
)
from webhook_agent.core.policy import Mode, resolve_event_policy
from webhook_agent.webhook_types import ActionResult

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_post_conversational_reply_success() -> None:
    """Test _post_conversational_reply successfully creates a comment on the issue."""
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_issue = mock_repo.get_issue.return_value
    mock_comment = MagicMock()
    mock_comment.html_url = "https://github.com/owner/repo/issues/10#issuecomment-1"
    mock_issue.create_comment.return_value = mock_comment

    results: list[ActionResult] = []
    with (
        patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.is_allowed", return_value=True),
        patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.record") as mock_record,
    ):
        _post_conversational_reply(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=10,
            emitted_texts=["First thought", "Hello author!"],
            trace_id="trace-1234",
            results=results,
        )

        mock_issue.create_comment.assert_called_once_with("First thought\n\nHello author!")
        mock_record.assert_called_once_with("owner/repo#10")
        assert len(results) == 1
        assert results[0].tool == "add_comment"
        assert results[0].success is True


def test_post_conversational_reply_rate_limited() -> None:
    """Test _post_conversational_reply skips comment creation when rate limited."""
    mock_gh = MagicMock()
    results: list[ActionResult] = []
    with patch("webhook_agent.core.execution._COMMENT_RATE_LIMITER.is_allowed", return_value=False):
        _post_conversational_reply(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=10,
            emitted_texts=["Hello!"],
            trace_id="trace-1234",
            results=results,
        )

        mock_gh.get_repo.assert_not_called()
        assert len(results) == 0


def test_submit_deterministic_review_with_session_state() -> None:
    """Test _submit_deterministic_review extracts payload from session state and submits review."""
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = mock_repo.get_pull.return_value

    final_session = MagicMock()
    final_session.state = {
        "code_review_analysis": {"verdict": "APPROVE", "executive_summary": "Solid design."}
    }

    results: list[ActionResult] = []
    with (
        patch(
            "webhook_agent.core.execution._submit_formal_review",
            return_value=("Formal review submitted", True),
        ) as mock_submit,
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.mark_completed"
        ) as mock_checkpoint,
    ):
        _submit_deterministic_review(
            gh_client=mock_gh,
            repo_full_name="owner/repo",
            pr_number=42,
            head_sha="commit-sha-123",
            canonical="pull_request.opened",
            final_session=final_session,
            emitted_texts=[],
            results=results,
        )

        mock_submit.assert_called_once()
        mock_checkpoint.assert_called_once_with("owner/repo", 42, "commit-sha-123")
        assert len(results) == 1
        assert results[0].tool == "review"
        assert results[0].success is True


def test_execute_agent_event_checkpoint_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test execute_agent_event short-circuits when commit checkpoint is already completed."""
    monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._build_user_message.return_value = MagicMock(parts=[MagicMock(text="Audit request")])
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    mock_gh = MagicMock()
    with patch(
        "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
        return_value={"status": "completed"},
    ):
        results = execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-test",
        )

        assert len(results) == 1
        assert results[0].tool == "review"
        assert results[0].success is True
        assert "already reviewed" in results[0].detail


def test_execute_agent_event_exhaustion_marks_checkpoint_rate_limited() -> None:
    """Test execute_agent_event marks checkpoint rate-limited only when retries/chain exhausted."""
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._build_user_message.return_value = MagicMock(parts=[MagicMock(text="Audit request")])
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"
    mock_agent._advance_model_chain.return_value = None  # Immediately exhausted chain
    mock_session = MagicMock()
    mock_session.state = {}
    mock_agent._session_service.get_session = AsyncMock(return_value=mock_session)
    mock_agent._session_service.create_session = AsyncMock()
    mock_agent._session_service.append_event = AsyncMock()
    mock_agent._session_service.user_state = {}

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    mock_gh = MagicMock()
    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.mark_rate_limited"
        ) as mock_mark_rl,
        patch(
            "webhook_agent.core.execution._execute_adk_runner",
            side_effect=Exception("429 Resource exhausted: please retry after 45s"),
        ),
        patch("webhook_agent.core.execution._is_transient_error", return_value=True),
        patch(
            "webhook_agent.core.execution.extract_rate_limit_details",
            return_value={"retry_after_seconds": 45.0},
        ),
    ):
        results = execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-exhaust",
        )

        assert any(r.tool == "plan" and not r.success for r in results)
        mock_mark_rl.assert_called_once_with(
            repo="owner/repo",
            pr_number=42,
            head_sha="sha-abc",
            error_message="429 Resource exhausted: please retry after 45s",
            retry_after_seconds=45.0,
        )


def test_execute_agent_event_transient_failover_success_does_not_mark_rate_limited() -> None:
    """Test execute_agent_event does NOT mark checkpoint rate-limited if a failover succeeds."""
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._build_user_message.return_value = MagicMock(parts=[MagicMock(text="Audit request")])
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"
    mock_agent._advance_model_chain.return_value = "gemini-2.5-pro"
    mock_session = MagicMock()
    mock_session.state = {}
    mock_agent._session_service.get_session = AsyncMock(return_value=mock_session)
    mock_agent._session_service.create_session = AsyncMock()
    mock_agent._session_service.append_event = AsyncMock()
    mock_agent._session_service.user_state = {}

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    mock_gh = MagicMock()
    call_count = 0

    async def _mock_runner(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("503 Service Unavailable")
        # second call succeeds

    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.mark_rate_limited"
        ) as mock_mark_rl,
        patch("webhook_agent.core.execution._execute_adk_runner", side_effect=_mock_runner),
        patch("webhook_agent.core.execution.asyncio.sleep"),
    ):
        results = execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-recover",
        )

        mock_mark_rl.assert_not_called()


def test_execute_agent_event_resumes_with_precompiled_dossier() -> None:
    """Test execute_agent_event uses precompiled dossier and skips _build_user_message on resume."""
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"
    mock_session = MagicMock()
    mock_session.state = {}
    mock_agent._session_service.get_session = AsyncMock(return_value=mock_session)
    mock_agent._session_service.create_session = AsyncMock()
    mock_agent._session_service.append_event = AsyncMock()
    mock_agent._session_service.user_state = {}

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
        "precompiled_dossier": "### Resumed Precompiled Findings",
    }

    mock_gh = MagicMock()
    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch("webhook_agent.core.execution._execute_adk_runner") as mock_runner,
    ):
        execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-resume",
        )

        mock_agent._build_user_message.assert_not_called()
        mock_runner.assert_called_once()
        user_message_arg = mock_runner.call_args.kwargs["user_message"]
        assert "### Resumed Precompiled Findings" in user_message_arg.parts[0].text
        assert event_data.get("deterministic_precompiled_ast") is True


def test_execute_agent_event_rate_limited_paused_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test execute_agent_event short-circuits when commit checkpoint is rate_limited and backoff active."""
    monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/42"

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    mock_gh = MagicMock()
    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value={"status": "rate_limited"},
        ),
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_resumable",
            return_value=None,
        ),
        patch("webhook_agent.core.execution._execute_adk_runner") as mock_runner,
    ):
        results = execute_agent_event(
            agent=mock_agent,
            event_data=event_data,
            gh_client=mock_gh,
            trace_id="trace-paused",
        )

        assert len(results) == 1
        assert results[0].tool == "review"
        assert results[0].success is True
        assert "paused" in results[0].detail
        mock_runner.assert_not_called()
        mock_agent._build_user_message.assert_not_called()


def test_retry_loop_resets_user_message_prompt(monkeypatch: pytest.MonkeyPatch):
    """Verify that user_message text is reset on each retry attempt to avoid cumulative duplication (P0-5)."""
    import time

    monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
    mock_agent = MagicMock()
    mock_agent._app_name = "test_app"
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._advance_model_chain.return_value = "gemini-3.5-flash-lite"
    mock_agent._runner = MagicMock()
    mock_agent._runner.app.name = "test_app"

    fixed_time = 1700000000.0
    # Mock session service
    mock_session = MagicMock()
    mock_session.state = {
        "last_review_timestamp": fixed_time - 5.0,
        "last_review_critique": "Fix line 10",
    }
    mock_agent._session_service.get_session = AsyncMock(return_value=mock_session)
    mock_agent._session_service.append_event = AsyncMock()
    mock_agent._session_service.user_state = {}

    # Initial user message
    part = MagicMock()
    part.text = "Original user message"
    user_msg = MagicMock()
    user_msg.parts = [part]
    mock_agent._build_user_message.return_value = user_msg

    call_count = 0
    captured_texts: list[str] = []

    async def fake_execute(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal call_count
        call_count += 1
        captured_texts.append(str(user_msg.parts[0].text))
        if call_count == 1:
            raise Exception("429 Resource exhausted: rate limit exceeded")
        return []

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {"pull_request": {"number": 1, "head": {"sha": "sha1"}}},
    }

    with (
        patch("webhook_agent.core.execution._execute_adk_runner", side_effect=fake_execute),
        patch("webhook_agent.core.execution.time.time", return_value=fixed_time),
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_resumable",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch("webhook_agent.core.execution.asyncio.sleep", new_callable=AsyncMock),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event_data, gh_client=MagicMock(), trace_id="tr1"
        )

    assert call_count == 2
    # Verify that notice / critique is NOT duplicated on the second attempt
    assert captured_texts[0].count("[YOUR PREVIOUS REVIEW CRITIQUE]") == 1
    assert captured_texts[1].count("[YOUR PREVIOUS REVIEW CRITIQUE]") == 1
    assert captured_texts[0] == captured_texts[1]


def test_deterministic_session_state_contains_ast_flag() -> None:
    """Verify deterministic precompiled AST flag and review eligibility are passed to session state."""
    mock_agent = MagicMock()
    mock_agent._normalize_model_name.side_effect = lambda x: x
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._app_name = "test_app"

    session_service = MagicMock()
    mock_agent._session_service = session_service
    created_session = MagicMock()
    created_session.state = {}
    session_service.get_session = AsyncMock(side_effect=[None, created_session])
    session_service.create_session = AsyncMock(return_value=created_session)
    session_service.user_state = {}

    part = MagicMock()
    part.text = "Original user message"
    user_msg = MagicMock()
    user_msg.parts = [part]
    mock_agent._build_user_message.return_value = user_msg

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "deterministic_precompiled_ast": True,
        "raw_payload": {"pull_request": {"number": 1, "head": {"sha": "sha1"}}},
    }

    with (
        patch(
            "webhook_agent.core.execution._execute_adk_runner",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event_data, gh_client=MagicMock(), trace_id="tr1"
        )

    session_service.create_session.assert_called_once()
    state_passed = session_service.create_session.call_args.kwargs.get("state") or {}
    assert state_passed.get("deterministic_precompiled_ast") is True
    assert state_passed.get("formal_review_eligible") is True


def test_execute_agent_event_scopes_workspace_during_execution(tmp_path: Path) -> None:
    """Verify execute_agent_event activates pr_workspace around prompt building and runner execution."""
    from contextlib import asynccontextmanager

    from webhook_agent.environment import LocalEnvironment, get_active_environment

    mock_agent = MagicMock()
    mock_agent._normalize_model_name.side_effect = lambda x: x
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._app_name = "test_app"

    session_service = MagicMock()
    mock_agent._session_service = session_service
    created_session = MagicMock()
    created_session.state = {}
    session_service.get_session = AsyncMock(return_value=created_session)
    session_service.append_event = AsyncMock()
    session_service.user_state = {}

    env = LocalEnvironment(tmp_path)
    active_env_during_prompt: Any = None
    active_env_during_runner: Any = None

    def fake_build_message(data: Any) -> Any:
        nonlocal active_env_during_prompt
        active_env_during_prompt = get_active_environment()
        part = MagicMock()
        part.text = "prompt"
        msg = MagicMock()
        msg.parts = [part]
        return msg

    mock_agent._build_user_message.side_effect = fake_build_message

    async def fake_execute(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal active_env_during_runner
        active_env_during_runner = get_active_environment()
        return []

    @asynccontextmanager
    async def fake_pr_workspace(gh: Any, repo: str, sha: str):
        yield env

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {"pull_request": {"number": 1, "head": {"sha": "sha_pr_head"}}},
    }

    with (
        patch("webhook_agent.core.execution.pr_workspace", side_effect=fake_pr_workspace),
        patch("webhook_agent.core.execution._execute_adk_runner", side_effect=fake_execute),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event_data, gh_client=MagicMock(), trace_id="tr1"
        )

    assert active_env_during_prompt == env
    assert active_env_during_runner == env
    assert get_active_environment() is None


def test_search_files_during_agent_event_resolves_inside_pr_workspace(tmp_path: Path) -> None:
    """Verify search_files executed during an agent event resolves files inside the PR workspace."""
    from contextlib import asynccontextmanager

    from webhook_agent.environment import LocalEnvironment
    from webhook_agent.tools.search_tools import search_files

    mock_agent = MagicMock()
    mock_agent._normalize_model_name.side_effect = lambda x: x
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._app_name = "test_app"

    session_service = MagicMock()
    mock_agent._session_service = session_service
    created_session = MagicMock()
    created_session.state = {}
    session_service.get_session = AsyncMock(return_value=created_session)
    session_service.append_event = AsyncMock()
    session_service.user_state = {}

    # Write a unique marker file inside tmp_path (PR workspace)
    pr_file = tmp_path / "pr_unique_marker.py"
    pr_file.write_text("UNIQUE_PR_WORKSPACE_TARGET_TOKEN = 12345\n")

    env = LocalEnvironment(tmp_path)
    search_results: list[dict[str, Any]] = []

    async def fake_execute(*args: Any, **kwargs: Any) -> list[Any]:
        # Tool call executed by runner
        res = await search_files(pattern="UNIQUE_PR_WORKSPACE_TARGET_TOKEN")
        search_results.append(res)
        return []

    @asynccontextmanager
    async def fake_pr_workspace(gh: Any, repo: str, sha: str):
        yield env

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {"pull_request": {"number": 1, "head": {"sha": "head123"}}},
    }

    with (
        patch("webhook_agent.core.execution.pr_workspace", side_effect=fake_pr_workspace),
        patch("webhook_agent.core.execution._execute_adk_runner", side_effect=fake_execute),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event_data, gh_client=MagicMock(), trace_id="tr1"
        )

    assert len(search_results) == 1
    matches = search_results[0].get("matches", [])
    assert len(matches) == 1
    assert matches[0]["path"] == "pr_unique_marker.py"


@pytest.mark.anyio
async def test_execute_adk_runner_max_llm_calls_default_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _execute_adk_runner configures max_llm_calls default to 10 and respects env overrides."""
    from google.genai import types as genai_types

    captured_run_configs = []

    class FakeRunner:
        async def run_async(self, *args: Any, **kwargs: Any) -> Any:
            captured_run_configs.append(kwargs.get("run_config"))
            if False:
                yield None

    runner = FakeRunner()
    user_msg = genai_types.Content(parts=[genai_types.Part.from_text(text="Audit")])

    monkeypatch.delenv("MAX_AUDITOR_LLM_CALLS", raising=False)
    monkeypatch.delenv("ADK_MAX_LLM_CALLS", raising=False)
    await _execute_adk_runner(
        active_runner=runner,
        user_id="user1",
        session_id="sess1",
        user_message=user_msg,
        current_model_name="gemini-3.5-flash-lite",
        trace_id="tr1",
        results=[],
        emitted_texts=[],
    )
    assert len(captured_run_configs) == 1
    assert captured_run_configs[0].max_llm_calls == 10

    # Test override
    monkeypatch.setenv("MAX_AUDITOR_LLM_CALLS", "15")
    await _execute_adk_runner(
        active_runner=runner,
        user_id="user1",
        session_id="sess1",
        user_message=user_msg,
        current_model_name="gemini-3.5-flash-lite",
        trace_id="tr1",
        results=[],
        emitted_texts=[],
    )
    assert len(captured_run_configs) == 2
    assert captured_run_configs[1].max_llm_calls == 15


def test_agent_plugins_iteration_and_guardrail_budget() -> None:
    """Verify WebhookAgent initializes iteration budget to 10 turns and guardrail window to 4."""
    from webhook_agent.core.agent_definition import WebhookAgent

    agent = WebhookAgent(dry_run=True)
    assert agent._iteration_budget_plugin.max_turns == 10
    assert agent._iteration_budget_plugin.max_tool_calls == 25
    assert agent._guardrails_plugin.no_progress_window == 4


def test_second_sender_on_same_pr_shares_session_history() -> None:
    """Two events, same PR, different senders -> one shared session."""
    from google.adk.events import Event, EventActions
    from google.adk.sessions import InMemorySessionService

    session_service = InMemorySessionService()

    mock_agent = MagicMock()
    mock_agent._current_model_name = "gemini-3.5-flash-lite"
    mock_agent._normalize_model_name.return_value = "gemini-3.5-flash-lite"
    mock_agent._app_name = "test_app"
    mock_agent._session_service = session_service
    mock_agent._derive_session_id.return_value = "owner/repo/42"
    mock_agent._build_user_message.return_value = MagicMock(parts=[MagicMock(text="Audit request")])

    mock_runner = MagicMock()
    mock_runner.app = MagicMock(name="test_app")
    mock_runner.app.name = "test_app"
    mock_agent._runner = mock_runner
    mock_agent._conversational_runner = mock_runner

    # Event 1: pull_request.opened, sender=cgj8702-agents
    event1: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "cgj8702-agents"},
        "raw_payload": {
            "pull_request": {"number": 42, "head": {"sha": "sha-abc"}},
            "changed_files": ["src/app.py"],
        },
    }

    # Simulate runner executing and updating session state with review critique
    async def fake_execute_runner_1(**kwargs: Any) -> None:
        user_id = kwargs["user_id"]
        session_id = kwargs["session_id"]
        sess = await session_service.get_session(
            app_name="test_app", user_id=user_id, session_id=session_id
        )
        assert sess is not None
        await session_service.append_event(
            sess,
            Event(
                invocation_id="tr1",
                author="code_auditor",
                actions=EventActions(
                    state_delta={"review_critique": "Prior review critique for PR 42"}
                ),
            ),
        )

    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch(
            "webhook_agent.core.execution._execute_adk_runner", side_effect=fake_execute_runner_1
        ),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event1, gh_client=MagicMock(), trace_id="tr1"
        )

    # Event 2: issue_comment.created, sender=cgj8702 on the same PR 42
    event2: dict[str, Any] = {
        "canonical": "issue_comment.created",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "cgj8702"},
        "raw_payload": {
            "issue": {"number": 42},
            "comment": {"body": "Could you explain the critique?"},
        },
    }

    event2_session_state: dict[str, Any] = {}

    async def fake_execute_runner_2(**kwargs: Any) -> None:
        user_id = kwargs["user_id"]
        session_id = kwargs["session_id"]
        sess = await session_service.get_session(
            app_name="test_app", user_id=user_id, session_id=session_id
        )
        assert sess is not None
        event2_session_state.update(sess.state)

    with (
        patch(
            "webhook_agent.core.execution.review_checkpoint_manager.get_checkpoint",
            return_value=None,
        ),
        patch("webhook_agent.core.execution.review_checkpoint_manager.save_checkpoint"),
        patch(
            "webhook_agent.core.execution._execute_adk_runner", side_effect=fake_execute_runner_2
        ),
    ):
        execute_agent_event(
            agent=mock_agent, event_data=event2, gh_client=MagicMock(), trace_id="tr2"
        )

    # Event 2's session MUST contain Event 1's review findings
    assert event2_session_state.get("review_critique") == "Prior review critique for PR 42"

    # Confirm sender attribution survives in user_state_map as sender_login, not PR user_id
    user_state = session_service.user_state["test_app"]["owner/repo#42"]
    assert user_state["sender"] == "cgj8702"
    assert user_state["repo_full_name"] == "owner/repo"


def test_execute_agent_event_fast_fails_on_missing_repo() -> None:
    """Verify execute_agent_event fast-fails with ValueError when repo full_name is missing."""
    event: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "sender": {"login": "dev"},
        "raw_payload": {"pull_request": {"number": 1}},
    }
    with pytest.raises(ValueError, match="missing repository full_name"):
        execute_agent_event(
            agent=MagicMock(), event_data=event, gh_client=MagicMock(), trace_id="tr"
        )


def test_execute_agent_event_fast_fails_on_missing_pr_number() -> None:
    """Verify execute_agent_event fast-fails with ValueError when PR/issue number is missing."""
    event: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "dev"},
        "raw_payload": {"pull_request": {}},
    }
    with pytest.raises(ValueError, match="missing PR or issue number"):
        execute_agent_event(
            agent=MagicMock(), event_data=event, gh_client=MagicMock(), trace_id="tr"
        )


def test_execute_agent_event_fast_fails_on_missing_sender() -> None:
    """Verify execute_agent_event fast-fails with ValueError when sender login is missing."""
    event: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {},
        "raw_payload": {"pull_request": {"number": 1}},
    }
    with pytest.raises(ValueError, match="missing sender login"):
        execute_agent_event(
            agent=MagicMock(), event_data=event, gh_client=MagicMock(), trace_id="tr"
        )


def test_execute_agent_event_short_circuits_on_ignore_mode() -> None:
    """Verify execute_agent_event short-circuits immediately with empty results on Mode.IGNORE."""
    mock_agent = MagicMock()
    mock_agent._derive_session_id.return_value = "owner/repo/1"
    event: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "coderabbitai[bot]"},
        "raw_payload": {"pull_request": {"number": 1}},
    }
    results = execute_agent_event(
        agent=mock_agent, event_data=event, gh_client=MagicMock(), trace_id="tr-test"
    )
    assert results == []
    assert not mock_agent._runner.mock_calls
    assert not mock_agent._conversational_runner.mock_calls


def test_derive_session_id_fast_fails_without_fallback() -> None:
    """Verify WebhookAgent._derive_session_id fast-fails with ValueError instead of fallback GUID."""
    from webhook_agent.core.agent_definition import WebhookAgent

    agent = WebhookAgent(dry_run=True)

    # Valid PR
    pr_event = {
        "repository": {"full_name": "owner/repo"},
        "raw_payload": {"pull_request": {"number": 10}},
    }
    assert agent._derive_session_id(pr_event) == "owner/repo/10"

    # Valid Issue
    issue_event = {
        "repository": {"full_name": "owner/repo"},
        "raw_payload": {"issue": {"number": 20}},
    }
    assert agent._derive_session_id(issue_event) == "owner/repo/20"

    # Missing repo full_name
    missing_repo_event = {
        "raw_payload": {"pull_request": {"number": 10}},
    }
    with pytest.raises(ValueError, match="missing repository full_name"):
        agent._derive_session_id(missing_repo_event)

    # Missing PR/issue number (EV-C: no delivery GUID fallback allowed)
    no_number_event = {
        "repository": {"full_name": "owner/repo"},
        "delivery_id": "b1353d10-ca40-11f1-guid",
        "raw_payload": {"workflow_run": {"id": 123}},
    }
    with pytest.raises(ValueError, match="missing PR/issue number"):
        agent._derive_session_id(no_number_event)


class TestResolveEventPolicy:
    """Tests for resolve_event_policy() - the single source of truth for event policy."""

    def test_pull_request_opened_is_formal_audit(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702-agents"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.FORMAL_AUDIT
        assert policy.may_submit_review is True
        assert policy.may_post_comment is True
        assert policy.prefetch_diff is True
        assert policy.thinking_budget == 4096
        assert policy.session_key == "owner/repo#42"

    def test_pull_request_synchronize_is_formal_audit(self) -> None:
        event_data = {
            "canonical": "pull_request.synchronize",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702-agents"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.FORMAL_AUDIT
        assert policy.may_submit_review is True

    def test_pull_request_review_comment_is_thread_collab(self) -> None:
        event_data = {
            "canonical": "pull_request_review_comment.created",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702"},
            "raw_payload": {
                "comment": {"body": "Thanks for the review!", "thread_id": "thread-123"},
                "pull_request": {"number": 42},
            },
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.THREAD_COLLAB
        assert policy.may_post_comment is True
        assert policy.may_resolve_threads is True

    def test_issue_opened_is_issue_triage(self) -> None:
        event_data = {
            "canonical": "issues.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702"},
            "raw_payload": {"issue": {"number": 42}},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.ISSUE_TRIAGE
        assert policy.may_post_comment is True
        assert policy.may_create_issue is True

    def test_issue_comment_is_general_pair_programming(self) -> None:
        event_data = {
            "canonical": "issue_comment.created",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702"},
            "raw_payload": {
                "issue": {"number": 42, "pull_request": None},
                "comment": {"body": "How does this work?"},
            },
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.GENERAL_PAIR_PROGRAMMING
        assert policy.may_post_comment is True
        assert policy.may_submit_review is False

    def test_issue_comment_mentioning_coderabbit_is_ignored(self) -> None:
        for body in ("@coderabbitai review", "Hey @coderabbitai[bot] help", "@dependabot update"):
            event_data = {
                "canonical": "issue_comment.created",
                "repository": {"full_name": "owner/repo"},
                "sender": {"login": "cgj8702"},
                "raw_payload": {
                    "issue": {"number": 42, "pull_request": None},
                    "comment": {"body": body},
                },
            }
            policy = resolve_event_policy(event_data)
            assert policy.mode == Mode.IGNORE
            assert "comment targets third-party bot" in policy.reason

    def test_bot_event_is_ignore(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "coderabbitai[bot]"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.IGNORE
        assert policy.may_submit_review is False
        assert policy.may_post_comment is False

    def test_partner_bot_pull_request_opened_is_formal_audit(self) -> None:
        for bot_login in ("cline-cloud[bot]", "google-labs-jules[bot]"):
            event_data = {
                "canonical": "pull_request.opened",
                "repository": {"full_name": "owner/repo"},
                "sender": {"login": bot_login},
                "raw_payload": {"pull_request": {"number": 42}},
            }
            policy = resolve_event_policy(event_data)
            assert policy.mode == Mode.FORMAL_AUDIT
            assert policy.may_submit_review is True
            assert policy.session_key == "owner/repo#42"

    def test_partner_bot_comment_with_review_intent_is_formal_audit(self) -> None:
        event_data = {
            "canonical": "issue_comment.created",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "google-labs-jules[bot]"},
            "raw_payload": {
                "issue": {"number": 42, "pull_request": {}},
                "comment": {"body": "/review please"},
            },
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.FORMAL_AUDIT
        assert policy.may_submit_review is True

    def test_partner_bot_comment_without_review_intent_is_ignore(self) -> None:
        event_data = {
            "canonical": "issue_comment.created",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "google-labs-jules[bot]"},
            "raw_payload": {
                "issue": {"number": 42, "pull_request": {}},
                "comment": {"body": "I updated the files."},
            },
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.IGNORE

    def test_own_bot_event_is_ignore(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "hannibal-hub-agents[bot]"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.IGNORE

    def test_unknown_event_is_ignore(self) -> None:
        event_data = {
            "canonical": "unknown.event",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702"},
            "raw_payload": {},
        }
        policy = resolve_event_policy(event_data)
        assert policy.mode == Mode.IGNORE

    def test_dry_run_does_not_affect_mode(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702-agents"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        policy_dry = resolve_event_policy(event_data, dry_run=True)
        policy_live = resolve_event_policy(event_data, dry_run=False)
        assert policy_dry.mode == policy_live.mode == Mode.FORMAL_AUDIT

    def test_actionable_event_missing_repository_fast_fails(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "sender": {"login": "cgj8702-agents"},
            "raw_payload": {"pull_request": {"number": 42}},
        }
        with pytest.raises(ValueError, match="missing repository full_name"):
            resolve_event_policy(event_data)

    def test_actionable_event_missing_pr_number_fast_fails(self) -> None:
        event_data = {
            "canonical": "pull_request.opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "cgj8702-agents"},
            "raw_payload": {"pull_request": {}},
        }
        with pytest.raises(ValueError, match="missing PR/issue number"):
            resolve_event_policy(event_data)


class TestAuditorThinkingBudget:
    """Tests for get_auditor_thinking_budget parsing and fallbacks."""

    def test_default_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from webhook_agent.core.policy import (
            DEFAULT_AUDITOR_THINKING_BUDGET,
            get_auditor_thinking_budget,
        )

        monkeypatch.delenv("AUDITOR_THINKING_BUDGET", raising=False)
        assert get_auditor_thinking_budget() == DEFAULT_AUDITOR_THINKING_BUDGET

    def test_valid_integer_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from webhook_agent.core.policy import get_auditor_thinking_budget

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "8192")
        assert get_auditor_thinking_budget() == 8192

    def test_whitespace_and_invalid_overrides_fall_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from webhook_agent.core.policy import (
            DEFAULT_AUDITOR_THINKING_BUDGET,
            get_auditor_thinking_budget,
        )

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "")
        assert get_auditor_thinking_budget() == DEFAULT_AUDITOR_THINKING_BUDGET

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "   ")
        assert get_auditor_thinking_budget() == DEFAULT_AUDITOR_THINKING_BUDGET

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "4k")
        assert get_auditor_thinking_budget() == DEFAULT_AUDITOR_THINKING_BUDGET

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "auto")
        assert get_auditor_thinking_budget() == DEFAULT_AUDITOR_THINKING_BUDGET

    def test_zero_and_negative_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from webhook_agent.core.policy import get_auditor_thinking_budget

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "0")
        assert get_auditor_thinking_budget() == 0

        monkeypatch.setenv("AUDITOR_THINKING_BUDGET", "-1")
        assert get_auditor_thinking_budget() == -1


class TestToolGating:
    """Tests for mode-based tool gating in before_tool_callback."""

    def _make_tool_context(self, mode: Mode) -> Any:
        """Create a mock ToolContext with the given mode in session state."""
        ctx = MagicMock()
        ctx.state = {"agent_mode": mode}
        return ctx

    @pytest.mark.anyio
    async def test_review_tool_allowed_in_formal_audit(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "review"
        ctx = self._make_tool_context(Mode.FORMAL_AUDIT)
        result = await before_tool_callback(tool, {"pr_number": 42, "body": "{}"}, ctx)
        assert result is None  # Allowed

    @pytest.mark.anyio
    async def test_review_tool_rejected_in_thread_collab(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "review"
        ctx = self._make_tool_context(Mode.THREAD_COLLAB)
        result = await before_tool_callback(tool, {}, ctx)
        assert result is not None
        assert "error" in result
        assert "not permitted" in result["error"]

    @pytest.mark.anyio
    async def test_review_tool_rejected_in_issue_triage(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "review"
        ctx = self._make_tool_context(Mode.ISSUE_TRIAGE)
        result = await before_tool_callback(tool, {}, ctx)
        assert result is not None
        assert "error" in result
        assert "not permitted" in result["error"]

    @pytest.mark.anyio
    async def test_resolve_review_thread_allowed_in_thread_collab(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "resolve_review_thread"
        ctx = self._make_tool_context(Mode.THREAD_COLLAB)
        result = await before_tool_callback(tool, {}, ctx)
        assert result is None  # Allowed

    @pytest.mark.anyio
    async def test_resolve_review_thread_rejected_in_formal_audit(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "resolve_review_thread"
        ctx = self._make_tool_context(Mode.FORMAL_AUDIT)
        result = await before_tool_callback(tool, {}, ctx)
        assert result is not None
        assert "error" in result
        assert "not permitted" in result["error"]

    @pytest.mark.anyio
    async def test_read_file_allowed_in_all_modes(self) -> None:
        from webhook_agent.core.callbacks import before_tool_callback

        tool = MagicMock()
        tool.name = "read_file"
        for mode in [
            Mode.FORMAL_AUDIT,
            Mode.THREAD_COLLAB,
            Mode.ISSUE_TRIAGE,
            Mode.GENERAL_PAIR_PROGRAMMING,
            Mode.IGNORE,
        ]:
            ctx = self._make_tool_context(mode)
            result = await before_tool_callback(tool, {"file_path": "main.py"}, ctx)
            assert result is None, f"read_file should be allowed in mode {mode}"


@pytest.mark.anyio
async def test_execute_adk_runner_enforces_multi_turn_investigation_on_code_pr() -> None:
    """Verify _execute_adk_runner rejects single-turn review on code PR and enforces exploration."""
    from google.genai import types as genai_types

    calls = 0

    class MockRunner:
        async def run_async(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Turn 1: Model prematurely emits text without running tools
                yield MagicMock(
                    usage_metadata=None,
                    get_function_responses=list,
                    content=MagicMock(
                        parts=[MagicMock(text="Premature JSON review", thought=False)]
                    ),
                )
            elif calls == 2:
                # Turn 2: Enforced investigation, calls search_files and then emits final review
                fn_resp = MagicMock()
                fn_resp.name = "search_files"
                fn_resp.response = "matched 3 lines"
                yield MagicMock(
                    usage_metadata=None,
                    get_function_responses=lambda: [fn_resp],
                    content=None,
                )
                yield MagicMock(
                    usage_metadata=None,
                    get_function_responses=list,
                    content=MagicMock(
                        parts=[MagicMock(text="Final researched review", thought=False)]
                    ),
                )

    runner = MockRunner()
    results: list[ActionResult] = []
    emitted_texts: list[str] = []
    user_msg = genai_types.Content(parts=[genai_types.Part.from_text(text="Audit PR")])

    await _execute_adk_runner(
        active_runner=runner,
        user_id="user1",
        session_id="sess1",
        user_message=user_msg,
        current_model_name="gemini-3.5-flash-lite",
        trace_id="tr-test",
        results=results,
        emitted_texts=emitted_texts,
        is_code_audit=True,
    )

    assert calls == 2
    assert any(r.tool == "search_files" for r in results)
    assert emitted_texts == ["Final researched review"]


@pytest.mark.anyio
async def test_execute_adk_runner_allows_single_turn_when_not_code_audit() -> None:
    """Verify _execute_adk_runner allows single-turn output when is_code_audit is False."""
    from google.genai import types as genai_types

    calls = 0

    class MockRunner:
        async def run_async(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            yield MagicMock(
                usage_metadata=None,
                get_function_responses=list,
                content=MagicMock(parts=[MagicMock(text="Docs review", thought=False)]),
            )

    runner = MockRunner()
    results: list[ActionResult] = []
    emitted_texts: list[str] = []
    user_msg = genai_types.Content(parts=[genai_types.Part.from_text(text="Audit Docs PR")])

    await _execute_adk_runner(
        active_runner=runner,
        user_id="user1",
        session_id="sess1",
        user_message=user_msg,
        current_model_name="gemini-3.5-flash-lite",
        trace_id="tr-test-docs",
        results=results,
        emitted_texts=emitted_texts,
        is_code_audit=False,
    )

    assert calls == 1
    assert emitted_texts == ["Docs review"]


@pytest.mark.anyio
async def test_execute_adk_runner_single_turn_sufficient_if_tool_executed() -> None:
    """Verify _execute_adk_runner does not trigger follow-up turn if investigative tool was called in turn 1."""
    from google.genai import types as genai_types

    calls = 0

    class MockRunner:
        async def run_async(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            fn_resp = MagicMock()
            fn_resp.name = "read_file"
            fn_resp.response = "file content"
            yield MagicMock(
                usage_metadata=None,
                get_function_responses=lambda: [fn_resp],
                content=None,
            )
            yield MagicMock(
                usage_metadata=None,
                get_function_responses=list,
                content=MagicMock(parts=[MagicMock(text="Researched review", thought=False)]),
            )

    runner = MockRunner()
    results: list[ActionResult] = []
    emitted_texts: list[str] = []
    user_msg = genai_types.Content(parts=[genai_types.Part.from_text(text="Audit PR")])

    await _execute_adk_runner(
        active_runner=runner,
        user_id="user1",
        session_id="sess1",
        user_message=user_msg,
        current_model_name="gemini-3.5-flash-lite",
        trace_id="tr-test-tool",
        results=results,
        emitted_texts=emitted_texts,
        is_code_audit=True,
    )

    assert calls == 1
    assert any(r.tool == "read_file" for r in results)
    assert emitted_texts == ["Researched review"]
