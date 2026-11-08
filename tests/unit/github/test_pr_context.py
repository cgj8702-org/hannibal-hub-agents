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
    raw_with_intent = {
        "comment": {"body": "/review this PR"},
        "issue": {"pull_request": {"url": "pr_url"}},
    }
    assert _should_prefetch_diff("issue_comment.created", raw_with_intent) is True

    raw_issue_without_pr = {
        "comment": {"body": "/review this issue"},
        "issue": {"number": 42},
    }
    assert _should_prefetch_diff("issue_comment.created", raw_issue_without_pr) is False

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
    mock_pr.head.sha = "f" * 40
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
    # The diff snapshot must record the head it was taken from, so the auditor can
    # refuse to pair it with a mismatched event SHA.
    assert raw["diff_sha"] == "f" * 40


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


def test_prefetch_linked_issue_spec() -> None:
    from webhook_agent.github.pr_context import _prefetch_linked_issue_spec

    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_issue = MagicMock()
    mock_issue.pull_request = None
    mock_issue.title = "Memory leak in worker loop"
    mock_issue.body = "Worker crashes after 500 events."
    mock_repo.get_issue.return_value = mock_issue
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 42,
                "body": "This PR resolves #105 and cleans up memory.",
            }
        }
    }

    _prefetch_linked_issue_spec(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert "linked_issue_spec" in raw
    assert "Linked Issue #105: Memory leak in worker loop" in raw["linked_issue_spec"]
    assert "Worker crashes after 500 events." in raw["linked_issue_spec"]
    assert "UNTRUSTED USER SPECIFICATION" in raw["linked_issue_spec"]


def test_prefetch_linked_issue_spec_word_boundary_and_multi() -> None:
    from webhook_agent.github.pr_context import _prefetch_linked_issue_spec

    mock_gh = MagicMock()
    mock_repo = MagicMock()

    issue_1 = MagicMock()
    issue_1.pull_request = None
    issue_1.title = "First Bug"
    issue_1.body = "First bug details"

    issue_2 = MagicMock()
    issue_2.pull_request = None
    issue_2.title = "Second Bug"
    issue_2.body = "Second bug details"

    def get_issue(num: int):
        if num == 10:
            return issue_1
        if num == 20:
            return issue_2
        raise ValueError(f"Unexpected issue {num}")

    mock_repo.get_issue.side_effect = get_issue
    mock_gh.get_repo.return_value = mock_repo

    # 'prefixes #5' and 'unfixed #3' should NOT match due to word boundaries.
    # 'Fixes #10' and 'Closes: #20' SHOULD match.
    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 50,
                "body": "This has prefixes #5 and unfixed #3, but fixes #10 and closes: #20.",
            }
        }
    }

    _prefetch_linked_issue_spec(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert "linked_issue_spec" in raw
    assert "Linked Issue #10: First Bug" in raw["linked_issue_spec"]
    assert "Linked Issue #20: Second Bug" in raw["linked_issue_spec"]
    assert "#5" not in raw["linked_issue_spec"]
    assert "#3" not in raw["linked_issue_spec"]


def test_prefetch_previous_bot_reviews_ignores_other_bots() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_user_coderabbit = MagicMock()
    mock_user_coderabbit.login = "coderabbitai[bot]"
    mock_review_cr = MagicMock()
    mock_review_cr.user = mock_user_coderabbit
    mock_review_cr.state = "CHANGES_REQUESTED"
    mock_review_cr.body = "CodeRabbit review text."

    mock_pr.get_reviews.return_value = [mock_review_cr]
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "raw_payload": {
            "pull_request": {"number": 102},
        }
    }

    _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw["prior_reviews_had_request_changes"] is False
    assert "previous_bot_reviews" not in raw


def test_fetch_pr_raw_diff_via_requester() -> None:
    from webhook_agent.github.pr_context import fetch_pr_raw_diff

    mock_pr = MagicMock()
    mock_pr.url = "https://api.github.com/repos/owner/repo/pulls/1"
    mock_pr.raw_diff = None
    mock_requester = MagicMock()
    mock_requester.requestBlobAndCheck.return_value = (
        {},
        b"diff --git a/uv.lock b/uv.lock\n@@ -1,2 +1,2 @@\n-a\n+b\n",
    )
    mock_pr._requester = mock_requester

    diff = fetch_pr_raw_diff(mock_pr)
    assert "diff --git a/uv.lock b/uv.lock" in diff
    assert "+b" in diff


def test_fetch_pr_raw_diff_via_raw_override() -> None:
    from webhook_agent.github.pr_context import fetch_pr_raw_diff

    mock_pr = MagicMock()
    mock_pr.raw_diff = "diff --git a/file b/file\n"
    assert fetch_pr_raw_diff(mock_pr) == "diff --git a/file b/file\n"


def test_prefetch_pr_diff_falls_back_to_raw_diff_when_patch_none() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_file = MagicMock()
    mock_file.filename = "uv.lock"
    mock_file.status = "modified"
    mock_file.patch = None

    mock_pr.get_files.return_value = [mock_file]
    mock_pr.head.sha = "a" * 40
    mock_pr.state = "open"
    mock_pr.merged = False
    mock_pr.raw_diff = "diff --git a/uv.lock b/uv.lock\n@@ -1,2 +1,2 @@\n-old\n+new\n"

    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.opened",
        "raw_payload": {
            "pull_request": {"number": 277},
        },
    }

    _prefetch_pr_diff(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert "pr_diff" in raw
    assert "uv.lock" in raw["pr_diff"]
    assert "+new" in raw["pr_diff"]
    assert raw["diff_sha"] == "a" * 40
    assert raw["head_sha"] == "a" * 40
    assert payload["head_sha"] == "a" * 40


def test_prefetch_pr_diff_sets_head_sha_for_issue_comment() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_pr.get_files.return_value = []
    mock_pr.head.sha = "c" * 40
    mock_pr.state = "open"
    mock_pr.merged = False

    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "issue_comment.created",
        "raw_payload": {
            "issue": {
                "number": 284,
                "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/284"},
            },
            "comment": {"body": "/re-review pls"},
        },
    }

    _prefetch_pr_diff(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw["diff_sha"] == "c" * 40
    assert raw["head_sha"] == "c" * 40
    assert payload["head_sha"] == "c" * 40


def test_prefetch_pr_diff_synchronize_incremental() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    # Full PR files
    mock_file1 = MagicMock(filename="src/old.py", status="modified", patch="+print('old')")
    mock_file2 = MagicMock(filename="src/new.py", status="modified", patch="+print('new')")
    mock_pr.get_files.return_value = [mock_file1, mock_file2]
    mock_pr.state = "open"
    mock_pr.merged = False
    mock_pr.head.sha = "2" * 40

    # Incremental comparison: only src/new.py
    mock_inc_file = MagicMock(
        filename="src/new.py", status="modified", patch="+print('new incremental')"
    )
    mock_comparison = MagicMock(files=[mock_inc_file])
    mock_repo.compare.return_value = mock_comparison

    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "pull_request": {"number": 100},
            "before": "1" * 40,
        },
    }

    _prefetch_pr_diff(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw["is_incremental"] is True
    assert raw["incremental_base_sha"] == "1" * 40
    assert raw["incremental_head_sha"] == "2" * 40
    assert raw["changed_files"] == ["src/new.py"]
    assert "src/new.py" in raw["pr_diff"]
    assert "src/old.py" not in raw["pr_diff"]
    assert "src/old.py" in raw["full_pr_diff"]
    assert mock_repo.compare.call_count == 1


def test_prefetch_pr_diff_synchronize_incremental_fallback_on_compare_failure() -> None:
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_file = MagicMock(filename="src/full.py", status="modified", patch="+full")
    mock_pr.get_files.return_value = [mock_file]
    mock_pr.state = "open"
    mock_pr.merged = False
    mock_pr.head.sha = "2" * 40

    mock_repo.compare.side_effect = Exception("404 Not Found")
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "pull_request": {"number": 101},
            "before": "1" * 40,
        },
    }

    _prefetch_pr_diff(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw.get("is_incremental") is None or raw.get("is_incremental") is False
    assert raw["changed_files"] == ["src/full.py"]
    assert "src/full.py" in raw["pr_diff"]
    assert mock_repo.compare.call_count == 1
