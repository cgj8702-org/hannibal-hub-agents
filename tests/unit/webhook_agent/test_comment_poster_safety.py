"""Unit tests for auditor review comment safety and echo filtering.

Validates:
1. Echo suggestion detection (is_echo_suggestion) across single and multi-line diff slices.
2. Echo description detection (is_echo_description).
3. Diff-bomb guard in format_suggestion_body (multi-line without range anchor downgrades to ```python).
4. Multi-line range anchor support (start_line) in build_github_review_comments.
5. Filter echo suggestions helper (filter_echo_suggestions) for PR review summaries.
"""

from __future__ import annotations

import pytest

from webhook_agent.comment_poster import (
    build_github_review_comments,
    filter_echo_suggestions,
    format_suggestion_body,
    is_echo_description,
    is_echo_suggestion,
)
from webhook_agent.schemas import IssueItem

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_echo_suggestion_single_line():
    """Verify single-line exact echo detection against diff line."""
    file_lines = {
        10: "    x = 42",
        11: "    return x",
    }
    # Exact match with varying indentation or whitespace
    assert is_echo_suggestion("    x = 42", file_lines, line=10) is True
    assert is_echo_suggestion("x = 42", file_lines, line=10) is True
    assert is_echo_suggestion("```python\nx = 42\n```", file_lines, line=10) is True

    # Real modification
    assert is_echo_suggestion("x = 99", file_lines, line=10) is False
    assert is_echo_suggestion("y = 42", file_lines, line=10) is False


def test_is_echo_suggestion_multiline_slice():
    """Verify multi-line slice echo detection starting at target line."""
    file_lines = {
        546: "    # 5. Classify quota_type: RPD vs TPM vs RPM",
        547: "    check_str = (",
        548: "        f\"{details.get('quota_id') or ''} {details.get('message') or ''}\"",
        549: "    ).lower()",
        550: '    if "perday" in check_str:',
        551: '        details["quota_type"] = "RPD"',
    }
    suggested_echo = """
    # 5. Classify quota_type: RPD vs TPM vs RPM
    check_str = (
        f"{details.get('quota_id') or ''} {details.get('message') or ''}"
    ).lower()
    if "perday" in check_str:
        details["quota_type"] = "RPD"
    """
    assert is_echo_suggestion(suggested_echo, file_lines, line=546) is True

    # Modified suggestion should not be flagged as an echo
    suggested_diff = """
    # 5. Classify quota_type: RPD vs TPM vs RPM
    check_str = (
        f"{details.get('quota_id') or ''} {details.get('message') or ''}"
    ).lower()
    if "perday" in check_str or "daily" in check_str:
        details["quota_type"] = "RPD"
    """
    assert is_echo_suggestion(suggested_diff, file_lines, line=546) is False


def test_is_echo_suggestion_with_explicit_start_line():
    """Verify echo detection across explicit [start_line, line] range."""
    file_lines = {
        10: "def calculate(a, b):",
        11: "    return a + b",
    }
    assert (
        is_echo_suggestion(
            "def calculate(a, b):\n    return a + b", file_lines, line=11, start_line=10
        )
        is True
    )
    assert (
        is_echo_suggestion(
            "def calculate(a, b):\n    return a * b", file_lines, line=11, start_line=10
        )
        is False
    )


def test_is_echo_description():
    """Verify detection of observational echo descriptions."""
    assert is_echo_description("Ensure edge-case combinations prioritize tokens correctly.") is True
    assert is_echo_description("Verify that the return value is not null.") is True
    assert (
        is_echo_description(
            "Classification checks string formatting for quota classification. Ensure edge-cases..."
        )
        is True
    )

    # Actionable defect descriptions
    assert is_echo_description("Missing error handling causes UnboundLocalError.") is False
    assert is_echo_description("Potential division by zero when denominator is empty.") is False


def test_format_suggestion_body_diff_bomb_protection():
    """Verify format_suggestion_body prevents single-line replacement diff bombs."""
    multiline_code = "if not item:\n    return None\nreturn item.process()"

    # Single-line anchor without range: must downgrade to ```python
    body_single_anchor = format_suggestion_body(
        "Guard against missing item", multiline_code, is_multiline_anchor=False
    )
    assert "```python\n" in body_single_anchor
    assert "```suggestion" not in body_single_anchor

    # Multi-line range anchor: allows ```suggestion
    body_multiline_anchor = format_suggestion_body(
        "Guard against missing item", multiline_code, is_multiline_anchor=True
    )
    assert "```suggestion\n" in body_multiline_anchor

    # Single-line code always allows ```suggestion
    body_single_line = format_suggestion_body(
        "Use 42 instead", "val = 42", is_multiline_anchor=False
    )
    assert "```suggestion\nval = 42\n```" in body_single_line


def test_build_github_review_comments_suppresses_echo():
    """Verify build_github_review_comments suppresses echo findings completely."""
    diff = (
        "--- a/src/quota.py\n"
        "+++ b/src/quota.py\n"
        "@@ -10,3 +10,3 @@\n"
        "+    if limit > 0:\n"
        "+        process(limit)\n"
        "+    return limit\n"
    )
    # An issue that echoes existing line 10 with an observational description
    echo_issue = IssueItem(
        path="src/quota.py",
        line=10,
        description="Ensure limit is greater than 0 before processing.",
        suggested_fix="if limit > 0:",
    )
    # A real issue proposing a modification
    real_issue = IssueItem(
        path="src/quota.py",
        line=11,
        description="Add error handling around process call",
        suggested_fix="try:\n    process(limit)\nexcept Exception:\n    pass",
    )

    comments, keys = build_github_review_comments([echo_issue, real_issue], diff)

    # Echo issue was suppressed; only real issue was included
    assert len(comments) == 1
    assert comments[0]["line"] == 11
    assert "src/quota.py:10" not in keys
    assert "src/quota.py:11" in keys
    # Multi-line fix on line 11 without start_line is protected with ```python
    assert "```python" in comments[0]["body"]
    assert "```suggestion" not in comments[0]["body"]


def test_build_github_review_comments_multiline_range_anchor():
    """Verify multi-line range anchors include start_line and start_side in comment payload."""
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -5,5 +5,5 @@\n"
        "+def compute():\n"
        "+    a = 1\n"
        "+    b = 2\n"
        "+    return a + b\n"
    )
    range_issue = IssueItem(
        path="src/calc.py",
        start_line=6,
        line=8,
        description="Simplify addition computation",
        suggested_fix="return 1 + 2",
    )

    comments, keys = build_github_review_comments([range_issue], diff)
    assert len(comments) == 1
    assert "src/calc.py:8" in keys
    comment = comments[0]
    assert comment["line"] == 8
    assert comment["start_line"] == 6
    assert comment["side"] == "RIGHT"
    assert comment["start_side"] == "RIGHT"
    assert "```suggestion\nreturn 1 + 2\n```" in comment["body"]


def test_filter_echo_suggestions():
    """Verify filter_echo_suggestions strips echo issues before review summary rendering."""
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n+x = 10\n+y = 20\n"
    issues = [
        IssueItem(
            path="src/app.py",
            line=1,
            description="Ensure x is 10",
            suggested_fix="x = 10",
        ),
        IssueItem(
            path="src/app.py",
            line=2,
            description="Fix y initialization",
            suggested_fix="y = 30",
        ),
    ]

    filtered = filter_echo_suggestions(issues, diff)
    assert len(filtered) == 1
    assert filtered[0].line == 2
    assert filtered[0].suggested_fix == "y = 30"


def test_is_echo_suggestion_forward_non_empty_line_scan():
    """Verify forward non-empty line scan matches when interleaved with empty lines."""
    file_lines = {
        50: "def calculate():",
        51: "    ",
        52: "    a = 10",
        53: "",
        54: "    return a",
    }
    suggestion = "def calculate():\n    a = 10\n    return a"
    assert is_echo_suggestion(suggestion, file_lines, line=50) is True
