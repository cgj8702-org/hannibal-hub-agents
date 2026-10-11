"""Unit tests for gated GitHub ops tools (merge_pr, update_issue)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _make_mock_ctx(
    actor: str = "maintainer_user",
    author_association: str = "COLLABORATOR",
    repo_name: str = "org/repo",
) -> MagicMock:
    ctx = MagicMock()
    ctx.state = {
        "gh_client": MagicMock(),
        "repo_full_name": repo_name,
        "sender": actor,
        "author_association": author_association,
    }
    return ctx


def test_merge_pr_refuses_non_maintainer(monkeypatch):
    """merge_pr must refuse callers who are not maintainers/collaborators."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import merge_pr

    ctx = _make_mock_ctx(actor="external_user", author_association="CONTRIBUTOR")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_repo.get_collaborator_permission.return_value = "read"

    res = merge_pr(ctx, pr_number=1, expected_base="main")
    assert "Error" in res
    assert (
        "not authorized" in res.lower()
        or "maintainer" in res.lower()
        or "collaborator" in res.lower()
    )


def test_merge_pr_refuses_disabled_mutations(monkeypatch):
    """merge_pr must refuse execution when ALLOW_AUTOMATED_MUTATIONS is '0'."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "0")
    from webhook_agent.tools.github_tools import merge_pr

    ctx = _make_mock_ctx(actor="maintainer_user", author_association="MEMBER")
    res = merge_pr(ctx, pr_number=1, expected_base="main")
    assert "Error" in res
    assert "disabled" in res.lower() or "allow_automated_mutations" in res.lower()


def test_merge_pr_refuses_base_branch_mismatch(monkeypatch):
    """merge_pr must refuse when target PR base branch differs from expected_base."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import merge_pr

    ctx = _make_mock_ctx(actor="maintainer_user", author_association="MEMBER")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = mock_repo.get_pull.return_value
    mock_pr.base.ref = "develop"

    res = merge_pr(ctx, pr_number=1, expected_base="main")
    assert "Error" in res
    assert "base" in res.lower() or "mismatch" in res.lower()


def test_merge_pr_refuses_unapproved_or_failing_checks(monkeypatch):
    """merge_pr must refuse when review state is not APPROVED or check runs are failing."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import merge_pr

    ctx = _make_mock_ctx(actor="maintainer_user", author_association="MEMBER")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = mock_repo.get_pull.return_value
    mock_pr.base.ref = "main"
    mock_pr.head.sha = "abcdef123456"

    # Mock unapproved review
    mock_review = MagicMock()
    mock_review.state = "CHANGES_REQUESTED"
    mock_pr.get_reviews.return_value = [mock_review]

    res = merge_pr(ctx, pr_number=1, expected_base="main")
    assert "Error" in res
    assert any(k in res.lower() for k in ("approval", "review", "check", "changes", "requested"))


def test_merge_pr_guards_null_status_sha_and_logs_audit(monkeypatch, caplog):
    """merge_pr must handle null status.sha gracefully and emit audit log."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import merge_pr

    ctx = _make_mock_ctx(actor="maintainer_user", author_association="MEMBER")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = mock_repo.get_pull.return_value
    mock_pr.base.ref = "main"
    mock_pr.head.sha = "1234567890ab"

    # Mock approved review
    mock_review = MagicMock()
    mock_review.state = "APPROVED"
    mock_pr.get_reviews.return_value = [mock_review]

    # Mock green check runs and combined status
    mock_commit = mock_repo.get_commit.return_value
    mock_commit.get_combined_status.return_value.state = "success"
    mock_check_run = MagicMock()
    mock_check_run.conclusion = "success"
    mock_commit.get_check_runs.return_value = [mock_check_run]

    # Mock merge status with null sha
    mock_merge_status = MagicMock()
    mock_merge_status.merged = True
    mock_merge_status.sha = None
    mock_pr.merge.return_value = mock_merge_status

    with caplog.at_level("INFO"):
        res = merge_pr(ctx, pr_number=1, expected_base="main")

    assert "Successfully merged" in res
    assert "AUDIT" in caplog.text or "merge_pr" in caplog.text


def test_update_issue_refuses_non_maintainer(monkeypatch):
    """update_issue must refuse callers who are not maintainers/collaborators."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import update_issue

    ctx = _make_mock_ctx(actor="external_user", author_association="NONE")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_repo.get_collaborator_permission.return_value = "none"

    res = update_issue(ctx, number=10, title="New Title")
    assert "Error" in res
    assert (
        "not authorized" in res.lower()
        or "maintainer" in res.lower()
        or "collaborator" in res.lower()
    )


def test_update_issue_happy_path_and_audit_log(monkeypatch, caplog):
    """update_issue happy path updates title/body/state and logs audit entry."""
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    from webhook_agent.tools.github_tools import update_issue

    ctx = _make_mock_ctx(actor="maintainer_user", author_association="OWNER")
    mock_gh = ctx.state["gh_client"]
    mock_repo = mock_gh.get_repo.return_value
    mock_issue = mock_repo.get_issue.return_value
    mock_issue.number = 10

    with caplog.at_level("INFO"):
        res = update_issue(ctx, number=10, title="Updated Title", state="closed")

    assert "Successfully updated issue #10" in res
    mock_issue.edit.assert_called_once_with(title="Updated Title", state="closed")
    assert "AUDIT" in caplog.text or "update_issue" in caplog.text
