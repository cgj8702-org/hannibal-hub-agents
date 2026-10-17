"""Unit tests for GitHub GraphQL review thread resolver (webhook_agent.github.thread_resolver)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from webhook_agent.github.thread_resolver import (
    ReviewComment,
    ReviewThread,
    auto_resolve_coderabbit_threads,
    extract_token_from_gh,
    fetch_pr_review_threads,
    is_coderabbit_auto_resolve_enabled,
    resolve_review_thread,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_extract_token_from_gh() -> None:
    """Verify token extraction across token strings, Github objects, and PullRequests."""
    # 1. Plain string token
    assert extract_token_from_gh("ghp_test123") == "ghp_test123"
    assert extract_token_from_gh("  ghp_trimmed  ") == "ghp_trimmed"
    assert extract_token_from_gh("") is None

    # 2. PyGithub Github object with requester.auth.token
    mock_gh = MagicMock()
    mock_gh.requester.auth.token = "ghs_from_requester"
    assert extract_token_from_gh(mock_gh) == "ghs_from_requester"

    # 3. PyGithub model object with _requester.auth.token (e.g. PullRequest)
    mock_pr = MagicMock(spec=["_requester"])
    mock_pr._requester = MagicMock()
    mock_pr._requester.auth.token = "ghs_from_pr_requester"
    assert extract_token_from_gh(mock_pr) == "ghs_from_pr_requester"

    # 4. Unknown object
    assert extract_token_from_gh(12345) is None


def test_fetch_pr_review_threads_success() -> None:
    """Verify fetching and parsing GraphQL review threads."""
    sample_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "PRRT_1",
                                "isResolved": False,
                                "path": "src/module.py",
                                "line": 42,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_1",
                                            "databaseId": 101,
                                            "body": "Consider using explicit types.",
                                            "author": {"login": "coderabbitai"},
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "PRRT_2",
                                "isResolved": True,
                                "path": "tests/test_module.py",
                                "line": 10,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_2",
                                            "databaseId": 102,
                                            "body": "Fixed test case.",
                                            "author": {"login": "cgj8702"},
                                        }
                                    ]
                                },
                            },
                        ]
                    }
                }
            }
        }
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = sample_response
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        threads = fetch_pr_review_threads("cgj8702-org", "repo", 42, "token123")

    assert len(threads) == 2
    assert threads[0].id == "PRRT_1"
    assert threads[0].is_resolved is False
    assert threads[0].path == "src/module.py"
    assert threads[0].line == 42
    assert len(threads[0].comments) == 1
    assert threads[0].comments[0].author_login == "coderabbitai"
    assert threads[0].first_comment.author_login == "coderabbitai"
    assert "coderabbitai" in threads[0].authors

    assert threads[1].id == "PRRT_2"
    assert threads[1].is_resolved is True

    # Assert request headers
    call_args = mock_post.call_args
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer token123"


def test_fetch_pr_review_threads_graphql_errors() -> None:
    """Verify GraphQL error responses are handled gracefully."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errors": [{"message": "Resource not accessible"}]}
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        threads = fetch_pr_review_threads("cgj8702-org", "repo", 42, "token123")

    assert threads == []


def test_fetch_pr_review_threads_http_error() -> None:
    """Verify HTTP client exceptions return empty list without crashing."""
    with patch("httpx.Client.post", side_effect=httpx.ConnectTimeout("Network timed out")):
        threads = fetch_pr_review_threads("cgj8702-org", "repo", 42, "token123")

    assert threads == []


def test_resolve_review_thread_success() -> None:
    """Verify resolving review thread via GraphQL mutation."""
    sample_response = {
        "data": {
            "resolveReviewThread": {
                "thread": {
                    "id": "PRRT_123",
                    "isResolved": True,
                }
            }
        }
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = sample_response
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        success = resolve_review_thread("PRRT_123", "token123")

    assert success is True


def test_resolve_review_thread_failure() -> None:
    """Verify resolve_review_thread returns False on GraphQL error."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errors": [{"message": "Could not resolve"}]}
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_resp):
        success = resolve_review_thread("PRRT_123", "token123")

    assert success is False


def test_auto_resolve_coderabbit_threads() -> None:
    """Verify auto_resolve_coderabbit_threads targets only open bot threads."""
    th_coderabbit_open = ReviewThread(
        id="PRRT_1",
        is_resolved=False,
        path="src/foo.py",
        line=10,
        comments=[
            ReviewComment(
                id="c1",
                database_id=1,
                body="Please fix this",
                author_login="coderabbitai",
            )
        ],
    )
    th_human_open = ReviewThread(
        id="PRRT_2",
        is_resolved=False,
        path="src/bar.py",
        line=20,
        comments=[
            ReviewComment(
                id="c2",
                database_id=2,
                body="What about this?",
                author_login="cgj8702",
            )
        ],
    )
    th_coderabbit_resolved = ReviewThread(
        id="PRRT_3",
        is_resolved=True,
        path="src/baz.py",
        line=30,
        comments=[
            ReviewComment(
                id="c3",
                database_id=3,
                body="Old resolved note",
                author_login="coderabbitai",
            )
        ],
    )

    with (
        patch(
            "webhook_agent.github.thread_resolver.fetch_pr_review_threads",
            return_value=[th_coderabbit_open, th_human_open, th_coderabbit_resolved],
        ),
        patch(
            "webhook_agent.github.thread_resolver.resolve_review_thread",
            return_value=True,
        ) as mock_resolve,
    ):
        resolved = auto_resolve_coderabbit_threads(
            "cgj8702-org/repo", 42, "token123", target_logins=("coderabbitai",)
        )

    # Only PRRT_1 should be resolved
    assert resolved == ["PRRT_1"]
    mock_resolve.assert_called_once_with(
        "PRRT_1", "token123", endpoint="https://api.github.com/graphql"
    )


def test_is_coderabbit_auto_resolve_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify environment variable toggle for auto-resolve."""
    monkeypatch.setenv("ENABLE_CODERABBIT_AUTO_RESOLVE", "true")
    assert is_coderabbit_auto_resolve_enabled() is True

    monkeypatch.setenv("ENABLE_CODERABBIT_AUTO_RESOLVE", "false")
    assert is_coderabbit_auto_resolve_enabled() is False


def test_github_tools_resolve_tools() -> None:
    """Verify ADK model-callable tools in github_tools.py."""
    from webhook_agent.tools.github_tools import resolve_bot_review_threads, resolve_review_thread

    mock_ctx = MagicMock()
    mock_ctx.state.get.side_effect = lambda k: {
        "repo_full_name": "cgj8702-org/repo",
        "gh_client": "mock_token_abc",
    }.get(k)

    # 1. Test resolve_review_thread tool
    with patch("webhook_agent.github.thread_resolver.resolve_review_thread", return_value=True):
        res = resolve_review_thread(mock_ctx, "PRRT_999")
        assert "Successfully resolved review thread PRRT_999." in res

    # 2. Test resolve_bot_review_threads tool
    with patch(
        "webhook_agent.github.thread_resolver.auto_resolve_coderabbit_threads",
        return_value=["PRRT_999"],
    ):
        res_bot = resolve_bot_review_threads(mock_ctx, 42, bot_login="coderabbitai")
        assert (
            "Successfully resolved 1 review thread(s) from coderabbitai on cgj8702-org/repo#42: PRRT_999."
            in res_bot
        )
