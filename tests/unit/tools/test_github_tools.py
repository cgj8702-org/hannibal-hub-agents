"""Unit tests for GitHub model-callable tools."""

from unittest.mock import MagicMock

import pytest

from webhook_agent.tools.github_tools import create_issue

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_create_issue_disallowed_by_policy() -> None:
    """create_issue returns error when may_create_issue is False."""
    ctx = MagicMock()
    ctx.state = {"may_create_issue": False}

    res = create_issue(ctx, title="Test Issue", body="Issue body")
    assert "Error creating issue" in res
    assert "disallowed by policy" in res


def test_create_issue_allowed_by_policy() -> None:
    """create_issue creates issue when may_create_issue is True."""
    ctx = MagicMock()
    ctx.state = {
        "may_create_issue": True,
        "repo_full_name": "cgj8702/test-repo",
    }
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_issue = MagicMock()
    mock_issue.number = 42
    mock_issue.html_url = "https://github.com/cgj8702/test-repo/issues/42"
    mock_repo.create_issue.return_value = mock_issue
    mock_gh.get_repo.return_value = mock_repo
    ctx.state["gh_client"] = mock_gh

    res = create_issue(ctx, title="Test Issue", body="Issue body")
    assert "Successfully created issue #42" in res
    mock_repo.create_issue.assert_called_once_with(title="Test Issue", body="Issue body")
