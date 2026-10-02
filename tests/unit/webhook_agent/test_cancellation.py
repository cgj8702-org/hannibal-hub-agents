"""Unit tests for PRClosedRegistry and AbortAgentExecution short-circuiting."""

from __future__ import annotations

import pytest

from webhook_agent.callbacks import _check_pr_closed_short_circuit
from webhook_agent.cancellation import AbortAgentExecution, pr_closed_registry

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_pr_closed_registry_mark_and_check():
    """Verify marking a PR closed sets status in registry."""
    pr_closed_registry.mark_closed("cgj8702-org/test-repo", 101)
    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 101) is True
    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 102) is False


def test_check_pr_closed_short_circuit_raises():
    """Verify _check_pr_closed_short_circuit raises AbortAgentExecution when PR is closed."""
    pr_closed_registry.mark_closed("cgj8702-org/test-repo", 202)

    state = {
        "repo_full_name": "cgj8702-org/test-repo",
        "pr_number": 202,
    }

    with pytest.raises(AbortAgentExecution) as exc_info:
        _check_pr_closed_short_circuit(state)

    assert "closed or merged" in str(exc_info.value)


def test_prefetch_pr_diff_marks_closed_pr():
    """Verify _prefetch_pr_diff inspects live PR state and marks closed PRs in registry."""
    from unittest.mock import MagicMock

    from webhook_agent.processor import _prefetch_pr_diff

    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = MagicMock()
    mock_pr.state = "closed"
    mock_pr.merged = True
    mock_repo.get_pull.return_value = mock_pr

    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "pull_request": {
                "number": 303,
                "state": "open",
                "merged": False,
            }
        },
    }

    _prefetch_pr_diff(mock_gh, "cgj8702-org/test-repo", payload)

    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 303) is True
    assert payload["raw_payload"]["pull_request"]["state"] == "closed"
    assert payload["raw_payload"]["pull_request"]["merged"] is True
    assert "pr_diff" not in payload["raw_payload"]


def test_process_event_skips_agent_run_on_closed_pr():
    """Verify process_event short-circuits before agent.run if PR is closed."""
    from unittest.mock import MagicMock

    from webhook_agent.processor import WebhookProcessor

    mock_agent = MagicMock()
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = MagicMock()
    mock_pr.state = "closed"
    mock_pr.merged = True
    mock_repo.get_pull.return_value = mock_pr

    processor = WebhookProcessor()
    processor._agent_core = mock_agent
    processor._gh = mock_gh

    payload = {
        "event_name": "pull_request",
        "action": "synchronize",
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "repository": {"full_name": "cgj8702-org/test-repo"},
            "pull_request": {
                "number": 404,
                "state": "open",
                "merged": False,
            },
        },
    }

    processor.process_event(payload)

    mock_agent.run.assert_not_called()
    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 404) is True


def test_review_tool_raises_abort_agent_execution_on_closed_pr():
    """Verify review tool raises AbortAgentExecution and registers closed PR."""
    from unittest.mock import MagicMock

    from webhook_agent.webhook_agent import review

    ctx = MagicMock()
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = MagicMock()
    mock_pr.state = "closed"
    mock_pr.merged = False
    mock_repo.get_pull.return_value = mock_pr
    ctx.state = {"gh_client": mock_gh, "repo_full_name": "cgj8702-org/test-repo"}

    with pytest.raises(AbortAgentExecution):
        review(ctx, pr_number=505, body="LGTM", event="APPROVE")

    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 505) is True


def test_plan_and_execute_skips_when_pr_in_registry():
    """Verify plan_and_execute immediately skips when PR is in pr_closed_registry."""
    from unittest.mock import MagicMock

    from webhook_agent.webhook_agent import WebhookAgent

    pr_closed_registry.mark_closed("cgj8702-org/test-repo", 606)

    agent = WebhookAgent()
    mock_gh = MagicMock()

    event_data = {
        "canonical": "pull_request.synchronize",
        "repo_name": "cgj8702-org/test-repo",
        "raw_payload": {
            "repository": {"full_name": "cgj8702-org/test-repo"},
            "pull_request": {
                "number": 606,
                "state": "open",
                "merged": False,
            },
        },
    }

    results = agent.plan_and_execute(
        event_data=event_data, gh_client=mock_gh, trace_id="trace-test"
    )
    assert len(results) == 1
    assert results[0].tool == "skip_closed_pr"
    assert results[0].success is True
