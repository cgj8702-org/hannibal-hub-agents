"""Unit tests for isolated Git Worktree conflict resolution module."""

from unittest.mock import MagicMock

import pytest

from webhook_agent.logic.genai_provider import (
    GenerateContentProvider,
    InteractionsProvider,
    get_text_generation_provider,
)
from webhook_agent.tools.resolve_conflicts import (
    _synthesize_conflict_resolution,
    resolve_merge_conflicts,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_synthesize_conflict_resolution_no_markers() -> None:
    content = "def foo():\n    return 'bar'\n"
    mock_client = MagicMock()
    result = _synthesize_conflict_resolution("foo.py", content, mock_client)
    assert result == content
    mock_client.models.generate_content.assert_not_called()


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_synthesize_conflict_resolution_with_markers() -> None:
    content = (
        "<<<<<<< HEAD\n"
        "def foo():\n"
        "    return 'head'\n"
        "=======\n"
        "def foo():\n"
        "    return 'base'\n"
        ">>>>>>> origin/main\n"
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "def foo():\n    return 'resolved'\n"
    mock_client.models.generate_content.return_value = mock_response

    result = _synthesize_conflict_resolution("foo.py", content, mock_client)
    assert result == "def foo():\n    return 'resolved'\n"
    mock_client.models.generate_content.assert_called_once()


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_text_generation_provider_defaults_to_generate_content() -> None:
    mock_client = MagicMock()
    provider = get_text_generation_provider(mock_client, use_interactions=False)

    assert isinstance(provider, GenerateContentProvider)


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_interactions_provider_normalizes_output_and_id() -> None:
    mock_client = MagicMock()
    mock_interaction = MagicMock(output_text="resolved", id="int_123")
    mock_client.interactions.create.return_value = mock_interaction
    provider = get_text_generation_provider(mock_client, use_interactions=True)

    result = provider.generate(model="gemini-3.8-flash", prompt="resolve this")

    assert isinstance(provider, InteractionsProvider)
    assert result.text == "resolved"
    assert result.interaction_id == "int_123"
    mock_client.interactions.create.assert_called_once_with(
        model="gemini-3.8-flash",
        input="resolve this",
    )


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_synthesize_conflict_resolution_can_use_interactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_USE_INTERACTIONS", "true")
    content = "<<<<<<< HEAD\nhead\n=======\nbase\n>>>>>>> origin/main\n"
    mock_client = MagicMock()
    mock_client.interactions.create.return_value = MagicMock(
        output_text="resolved\n",
        id="int_456",
    )

    result = _synthesize_conflict_resolution("foo.py", content, mock_client)

    assert result == "resolved\n"
    mock_client.interactions.create.assert_called_once()
    mock_client.models.generate_content.assert_not_called()


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_resolve_merge_conflicts_failure_handling(tmp_path) -> None:
    # Testing graceful failure handling on invalid repo path
    res = resolve_merge_conflicts(
        pr_number=999,
        head_branch="invalid-head",
        base_branch="invalid-base",
        genai_client=None,
        repo_root=tmp_path,
    )
    assert res["success"] is False
    assert "Failed to resolve merge conflicts" in res["detail"] or "git" in res["detail"].lower()
