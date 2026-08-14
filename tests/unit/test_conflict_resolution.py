"""Unit tests for isolated Git Worktree conflict resolution module."""

from __future__ import annotations

from unittest.mock import MagicMock

from webhook_agent.tools.resolve_conflicts import (
    _synthesize_conflict_resolution,
    resolve_merge_conflicts,
)


def test_synthesize_conflict_resolution_no_markers() -> None:
    content = "def foo():\n    return 'bar'\n"
    mock_client = MagicMock()
    result = _synthesize_conflict_resolution("foo.py", content, mock_client)
    assert result == content
    mock_client.models.generate_content.assert_not_called()


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
    assert (
        "Failed to resolve merge conflicts" in res["detail"]
        or "git" in res["detail"].lower()
    )
