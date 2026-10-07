"""Unit tests for revived GitHub operations tools (merge_pr, update_issue)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.tools.github_tools import merge_pr, update_issue

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _make_mock_ctx(repo_mock=None):
    ctx = MagicMock()
    mock_gh = MagicMock()
    if repo_mock is not None:
        mock_gh.get_repo.return_value = repo_mock
    ctx.state = {
        "gh_client": mock_gh,
        "repo_full_name": "owner/repo",
    }
    return ctx, mock_gh


class TestMergePr:
    def test_merge_pr_success(self):
        repo = MagicMock()
        pr = MagicMock()
        pr.draft = False
        pr.mergeable = True
        status = MagicMock()
        status.merged = True
        status.sha = "1234567890abcdef"
        pr.merge.return_value = status
        repo.get_pull.return_value = pr

        ctx, _ = _make_mock_ctx(repo)
        res = merge_pr(ctx, pr_number=42, merge_method="squash")

        assert "Successfully merged PR #42 via squash" in res
        pr.merge.assert_called_once_with(merge_method="squash")

    def test_merge_pr_draft_rejected(self):
        repo = MagicMock()
        pr = MagicMock()
        pr.draft = True
        repo.get_pull.return_value = pr

        ctx, _ = _make_mock_ctx(repo)
        res = merge_pr(ctx, pr_number=42)

        assert "is a draft and cannot be merged" in res
        pr.merge.assert_not_called()

    def test_merge_pr_unmergeable_rejected(self):
        repo = MagicMock()
        pr = MagicMock()
        pr.draft = False
        pr.mergeable = False
        repo.get_pull.return_value = pr

        ctx, _ = _make_mock_ctx(repo)
        res = merge_pr(ctx, pr_number=42)

        assert "has merge conflicts" in res
        pr.merge.assert_not_called()

    def test_merge_pr_api_failure(self):
        repo = MagicMock()
        pr = MagicMock()
        pr.draft = False
        pr.mergeable = True
        status = MagicMock()
        status.merged = False
        status.message = "Method Not Allowed"
        pr.merge.return_value = status
        repo.get_pull.return_value = pr

        ctx, _ = _make_mock_ctx(repo)
        res = merge_pr(ctx, pr_number=42)

        assert "Failed to merge PR #42: Method Not Allowed" in res

    def test_merge_pr_exception_handling(self):
        repo = MagicMock()
        repo.get_pull.side_effect = RuntimeError("API rate limit exceeded")

        ctx, _ = _make_mock_ctx(repo)
        res = merge_pr(ctx, pr_number=42)

        assert "Error merging PR #42: API rate limit exceeded" in res


class TestUpdateIssue:
    def test_update_issue_all_fields(self):
        repo = MagicMock()
        issue = MagicMock()
        repo.get_issue.return_value = issue

        ctx, _ = _make_mock_ctx(repo)
        res = update_issue(
            ctx,
            issue_number=10,
            title="New Title",
            body="New Body",
            state="closed",
            labels=["bug", "fixed"],
        )

        assert "Successfully updated issue #10." in res
        issue.edit.assert_called_once_with(
            title="New Title",
            body="New Body",
            state="closed",
            labels=["bug", "fixed"],
        )

    def test_update_issue_no_changes(self):
        repo = MagicMock()
        issue = MagicMock()
        repo.get_issue.return_value = issue

        ctx, _ = _make_mock_ctx(repo)
        res = update_issue(ctx, issue_number=10)

        assert "No changes specified for #10." in res
        issue.edit.assert_not_called()

    def test_update_issue_exception(self):
        repo = MagicMock()
        repo.get_issue.side_effect = ValueError("Issue not found")

        ctx, _ = _make_mock_ctx(repo)
        res = update_issue(ctx, issue_number=99, title="X")

        assert "Error updating issue #99: Issue not found" in res
