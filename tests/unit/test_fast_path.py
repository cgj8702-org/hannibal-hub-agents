"""Unit tests for fast-path evaluation and base branch merge detection (fast_path.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.fast_path import (
    evaluate_dependency_fast_path,
    is_base_branch_merge_sync,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_base_branch_merge_sync_fast_path_commit_msg() -> None:
    mock_gh = MagicMock()
    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "action": "synchronize",
            "pull_request": {
                "head": {"ref": "feature-x", "sha": "headsha123"},
                "base": {"ref": "main", "sha": "basesha456"},
            },
            "commits": [
                {
                    "id": "headsha123",
                    "message": "Merge branch 'main' into feature-x",
                }
            ],
        },
    }

    assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is True


def test_is_base_branch_merge_sync_two_parents_api() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_commit = MagicMock()
    p1 = MagicMock()
    p1.sha = "basesha456"
    p2 = MagicMock()
    p2.sha = "prevhead123"
    mock_commit.parents = [p1, p2]
    mock_commit.commit = MagicMock()
    mock_commit.commit.message = "Merge branch 'main' into feature-x"

    mock_repo.get_commit.return_value = mock_commit
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "action": "synchronize",
            "pull_request": {
                "head": {"ref": "feature-x", "sha": "headsha123"},
                "base": {"ref": "main", "sha": "basesha456"},
            },
            "commits": [],
        },
    }

    assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is True


def test_is_base_branch_merge_sync_single_parent_returns_false() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_commit = MagicMock()
    p1 = MagicMock()
    p1.sha = "prevhead123"
    mock_commit.parents = [p1]

    mock_repo.get_commit.return_value = mock_commit
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "action": "synchronize",
            "pull_request": {
                "head": {"ref": "feature-x", "sha": "headsha123"},
                "base": {"ref": "main", "sha": "basesha456"},
            },
            "commits": [],
        },
    }

    assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is False


def test_is_base_branch_merge_sync_non_synchronize_returns_false() -> None:
    mock_gh = MagicMock()
    payload = {
        "canonical": "pull_request.opened",
        "raw_payload": {
            "action": "opened",
            "pull_request": {
                "head": {"ref": "feature-x", "sha": "headsha123"},
                "base": {"ref": "main", "sha": "basesha456"},
            },
        },
    }
    assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is False


def test_evaluate_dependency_fast_path_dry_run_or_none() -> None:
    mock_gh = MagicMock()
    assert evaluate_dependency_fast_path(mock_gh, "o/r", {}, {}, {}, None, dry_run=False) is False
    assert evaluate_dependency_fast_path(mock_gh, "o/r", {}, {}, {}, 1, dry_run=True) is False


@patch("webhook_agent.fast_path.logger")
def test_evaluate_dependency_fast_path_pure_dependency_approval(mock_logger: MagicMock) -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {"sender": {"login": "dependabot[bot]"}}
    raw = {
        "changed_files": ["uv.lock"],
        "pr_diff": "diff --git a/uv.lock b/uv.lock\n...",
    }
    pr_data = {"head": {"ref": "dependabot/pip/urllib3-2.0.0"}}

    with (
        patch("webhook_agent.analysis.lockfile_validator.is_pure_dependency_pr", return_value=True),
        patch("webhook_agent.analysis.lockfile_validator.validate_lockfile_diff") as mock_val,
        patch(
            "webhook_agent.fast_path._submit_formal_review", return_value=("Approved", True)
        ) as mock_submit,
    ):
        mock_val_res = MagicMock()
        mock_val_res.should_approve = True
        mock_val_res.summary = "Valid lockfile bump"
        mock_val.return_value = mock_val_res

        handled = evaluate_dependency_fast_path(
            gh=mock_gh,
            repo_name="owner/repo",
            payload=payload,
            raw=raw,
            pr_data=pr_data,
            pr_number=42,
            dry_run=False,
        )

        assert handled is True
        mock_submit.assert_called_once()


def test_evaluate_dependency_fast_path_not_approved() -> None:
    mock_gh = MagicMock()
    payload = {"sender": {"login": "dependabot[bot]"}}
    raw = {
        "changed_files": ["uv.lock"],
        "pr_diff": "diff --git a/uv.lock b/uv.lock\n...",
    }
    pr_data = {"head": {"ref": "dependabot/pip/urllib3-2.0.0"}}

    with (
        patch("webhook_agent.analysis.lockfile_validator.is_pure_dependency_pr", return_value=True),
        patch("webhook_agent.analysis.lockfile_validator.validate_lockfile_diff") as mock_val,
    ):
        mock_val_res = MagicMock()
        mock_val_res.should_approve = False
        mock_val_res.summary = "Suspicious changes"
        mock_val_res.rejection_reason = "Extra files altered"
        mock_val.return_value = mock_val_res

        handled = evaluate_dependency_fast_path(
            gh=mock_gh,
            repo_name="owner/repo",
            payload=payload,
            raw=raw,
            pr_data=pr_data,
            pr_number=42,
            dry_run=False,
        )

        assert handled is False
