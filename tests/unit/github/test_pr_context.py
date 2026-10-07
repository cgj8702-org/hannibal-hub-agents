"""Unit tests for PR context prefetching helpers (pr_context.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.github.pr_context import (
    _prefetch_inline_comment_context,
    _prefetch_pr_diff,
    _prefetch_previous_bot_reviews,
    _should_prefetch_diff,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_should_prefetch_diff_canonical() -> None:
    assert _should_prefetch_diff("pull_request.opened", {}) is True
    assert _should_prefetch_diff("pull_request.synchronize", {}) is True
    assert _should_prefetch_diff("pull_request.ready_for_review", {}) is True
    assert _should_prefetch_diff("pull_request.closed", {}) is False
    assert _should_prefetch_diff("issues.opened", {}) is False


def test_should_prefetch_diff_comment_intent() -> None:
    raw_with_intent = {"comment": {"body": "please review this PR"}}
    assert _should_prefetch_diff("issue_comment.created", raw_with_intent) is True

    raw_no_intent = {"comment": {"body": "just saying hi"}}
    assert _should_prefetch_diff("issue_comment.created", raw_no_intent) is False

    raw_issue_pr = {"comment": {"body": "hello"}, "issue": {"pull_request": {"url": "pr_url"}}}
    assert _should_prefetch_diff("issue_comment.created", raw_issue_pr) is True


def test_prefetch_pr_diff_populates_payload() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()
    mock_file = MagicMock()
    mock_file.filename = "src/main.py"
    mock_file.status = "modified"
    mock_file.patch = "+print('hello')"

    mock_pr.state = "open"
    mock_pr.merged = False
    mock_pr.get_files.return_value = [mock_file]
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.opened",
        "raw_payload": {
            "pull_request": {"number": 42},
        },
    }

    _prefetch_pr_diff(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert "pr_diff" in raw
    assert "src/main.py" in raw["pr_diff"]
    assert raw["changed_files"] == ["src/main.py"]


def test_prefetch_inline_comment_context() -> None:
    payload = {
        "canonical": "pull_request_review_comment.created",
        "raw_payload": {
            "comment": {
                "path": "src/main.py",
                "diff_hunk": "@@ -1,3 +1,3 @@\n-old\n+new",
                "line": 10,
            }
        },
    }

    _prefetch_inline_comment_context(MagicMock(), "owner/repo", payload)

    raw = payload["raw_payload"]
    assert "inline_code_context" in raw
    assert "File: src/main.py (Line 10)" in raw["inline_code_context"]
    assert "@@ -1,3 +1,3 @@" in raw["inline_code_context"]


def test_prefetch_previous_bot_reviews() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_user = MagicMock()
    mock_user.login = "hannibal-hub-agents[bot]"

    mock_review = MagicMock()
    mock_review.user = mock_user
    mock_review.state = "CHANGES_REQUESTED"
    mock_review.body = "Please fix line 15: variable unused."

    mock_pr.get_reviews.return_value = [mock_review]
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "raw_payload": {
            "pull_request": {"number": 101},
        }
    }

    _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw["prior_reviews_had_request_changes"] is True
    assert "previous_bot_reviews" in raw
    assert "Please fix line 15" in raw["previous_bot_reviews"]
