"""Tests for review architecture features adapted from adk-samples.

Validates:
1. JSON unescaped quote repair and object salvage.
2. Last fenced block extraction.
3. Diff-window line snapping and drift correction.
4. Cheapness filter (NOT_CHEAP_MARKERS).
5. Implausible body bounds validation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.comment_poster import build_github_review_comments
from webhook_agent.formatter import (
    extract_json_payload,
    is_implausible_body,
    is_not_cheap_finding,
    normalize_code_review_dict,
)
from webhook_agent.logic.diff_filter import filter_review_diff
from webhook_agent.logic.duplicate_detector import (
    already_raised,
    build_exclusions,
    group_repeated_findings,
)
from webhook_agent.logic.review_budget import (
    compute_round_allowance,
    should_suppress_round,
    summarise_review_history,
)
from webhook_agent.schemas import IssueItem
from webhook_agent.tools.diff_tools import check_window, walk_right_side
from webhook_agent.webhook_agent import _enforce_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_unescaped_quote_repair():
    """Verify that unescaped double quotes inside code snippets are repaired."""
    malformed_json = """
    ```json
    {
      "executive_summary": "Autonomous PR audit report.",
      "critical_issues": [
        {
          "path": "src/app.py",
          "line": 42,
          "description": "Double quotes break JSON unless escaped",
          "suggested_fix": "assert res[\\"success\\"] is True",
          "window": "  42: assert res[\"success\"] is True"
        }
      ],
      "minor_suggestions": [],
      "risks_and_edge_cases": []
    }
    ```
    """
    extracted = extract_json_payload(malformed_json)
    assert extracted is not None
    assert isinstance(extracted, dict)
    assert len(extracted["critical_issues"]) == 1
    assert extracted["critical_issues"][0]["path"] == "src/app.py"


def test_last_fenced_block_prioritization():
    """Verify that the last fenced code block wins when an exploratory diff scan precedes it."""
    response_text = """
    Let me examine the diff first:
    ```python
    def example():
        pass
    ```

    The diff reveals an issue in `feature.py`.

    ```json
    {
      "executive_summary": "Real review finding.",
      "critical_issues": [
        {
          "path": "src/feature.py",
          "line": 15,
          "description": "Found a real division by zero.",
          "suggested_fix": "return x / (y or 1)"
        }
      ],
      "minor_suggestions": []
    }
    ```
    """
    extracted = extract_json_payload(response_text)
    assert extracted is not None
    assert extracted.get("executive_summary") == "Real review finding."
    assert extracted["critical_issues"][0]["line"] == 15


def test_object_salvage():
    """Verify that salvage recovers well-formed issue objects from malformed JSON."""
    broken_payload = (
        "[\n"
        '  {"path": "src/valid.py", "line": 10, "description": "Good finding", "suggested_fix": ""},\n'
        '  {"path": "src/broken.py", "line": 20, "description": unclosed string,\n'
        "]"
    )
    extracted = extract_json_payload(broken_payload)
    assert extracted is not None
    assert "critical_issues" in extracted
    assert len(extracted["critical_issues"]) == 1
    assert extracted["critical_issues"][0]["path"] == "src/valid.py"


def test_diff_window_line_snapping():
    """Verify that off-by-a-few line references snap to the matching diff line."""
    diff = (
        "--- a/src/math_ops.py\n"
        "+++ b/src/math_ops.py\n"
        "@@ -10,4 +10,6 @@ def compute(val):\n"
        "     base = 100\n"
        "+    factor = val * 2\n"
        "+    result = base / factor\n"
        "     return result\n"
    )

    anchors, text_by_line = walk_right_side(diff)
    assert "src/math_ops.py" in anchors
    assert 11 in anchors["src/math_ops.py"]
    assert 12 in anchors["src/math_ops.py"]

    # Model cited line 14 (off by 2), but provided code matching line 12
    window_snippet = "  12: result = base / factor"
    verified, snapped_line, reason = check_window(
        window_snippet, 14, text_by_line["src/math_ops.py"]
    )
    assert verified is True
    assert snapped_line == 12
    assert "moved to 12" in reason or "anchor corrected" in reason

    # Now verify build_github_review_comments uses this to anchor the comment
    issue = IssueItem(
        path="src/math_ops.py",
        line=14,  # off by 2
        description="Potential division by zero when factor is 0.",
        suggested_fix="result = base / (factor or 1)",
        window=window_snippet,
    )

    inline_comments, anchored_keys = build_github_review_comments([issue], diff)
    assert len(inline_comments) == 1
    assert inline_comments[0]["line"] == 12  # snapped to 12
    assert "src/math_ops.py:12" in anchored_keys


def test_cheapness_filter():
    """Verify that speculative findings requiring multi-file tracing are filtered out."""
    assert is_not_cheap_finding("trace the value across the codebase to ensure consistency") is True
    assert is_not_cheap_finding("grep the repo for any callers") is True
    assert is_not_cheap_finding("run the code to test edge cases") is True
    assert is_not_cheap_finding("read lines 11-12 and compare types") is False

    raw_data = {
        "executive_summary": "Audit report.",
        "critical_issues": [
            {
                "path": "src/service.py",
                "line": 50,
                "description": "Suspected issue if an attacker changes upstream state.",
                "suggested_fix": "",
                "verify_steps": "trace the value across the codebase to see if it escapes",
            },
            {
                "path": "src/service.py",
                "line": 60,
                "description": "Concrete null dereference when user is None.",
                "suggested_fix": "if user is None: return",
                "verify_steps": "read lines 59-60",
            },
        ],
        "minor_suggestions": [],
    }

    normalized = normalize_code_review_dict(raw_data)
    assert len(normalized["critical_issues"]) == 1
    assert normalized["critical_issues"][0]["line"] == 60


def test_implausible_body_bounds():
    """Verify that oversized bodies or massive unbroken token strings are rejected."""
    normal_desc = "Variable `user_id` might be None, causing an AttributeError."
    assert is_implausible_body(normal_desc) is False

    massive_token = "A" * 150
    assert is_implausible_body(f"Issue found in {massive_token}") is True

    oversized_body = "word " * 1000
    assert is_implausible_body(oversized_body) is True


def test_enforce_verdict_with_repaired_json():
    """End-to-end test verifying _enforce_verdict succeeds with repaired JSON input."""
    raw_payload = """
    Here is my diff analysis:
    Looking at flawed_feature.py, line 9 hardcodes a production secret.

    ```json
    {
      "executive_summary": "Security vulnerability detected.",
      "critical_issues": [
        {
          "path": "src/webhook_agent/flawed_feature.py",
          "line": 9,
          "description": "Hardcoded secret in config",
          "suggested_fix": "API_KEY = os.getenv(\"API_KEY\")",
          "window": "  9: API_KEY = \"DUMMY_KEY\""
        }
      ],
      "minor_suggestions": [],
      "risks_and_edge_cases": []
    }
    ```
    """
    mock_file = MagicMock()
    mock_file.filename = "src/webhook_agent/flawed_feature.py"
    mock_file.patch = '@@ -5,6 +5,8 @@\n+API_KEY = "DUMMY_KEY"\n'

    mock_pr = MagicMock()
    mock_pr.get_reviews.return_value = []
    mock_pr.get_files.return_value = [mock_file]

    rendered_md, verdict, inline_comments = _enforce_verdict(
        raw_payload, "REQUEST_CHANGES", pr=mock_pr
    )
    assert verdict == "REQUEST_CHANGES"
    assert "Hardcoded secret in config" in rendered_md
    assert len(inline_comments) == 1
    assert inline_comments[0]["path"] == "src/webhook_agent/flawed_feature.py"


def test_filter_review_diff_drops_lockfiles_and_snapshots():
    """Verify that filter_review_diff ignores lockfiles, snapshots, minified code, and binaries."""
    diff = (
        "diff --git a/uv.lock b/uv.lock\n"
        "--- a/uv.lock\n"
        "+++ b/uv.lock\n"
        "@@ -1,3 +1,3 @@\n"
        "-revision = 1\n"
        "+revision = 2\n"
        "diff --git a/tests/__snapshots__/out.snap b/tests/__snapshots__/out.snap\n"
        "--- a/tests/__snapshots__/out.snap\n"
        "+++ b/tests/__snapshots__/out.snap\n"
        "@@ -1,2 +1,2 @@\n"
        "-snapshot1\n"
        "+snapshot2\n"
        "diff --git a/static/bundle.min.js b/static/bundle.min.js\n"
        "--- a/static/bundle.min.js\n"
        "+++ b/static/bundle.min.js\n"
        "@@ -1 +1 @@\n"
        "-var a=1;\n"
        "+var a=2;\n"
        "diff --git a/src/core.py b/src/core.py\n"
        "--- a/src/core.py\n"
        "+++ b/src/core.py\n"
        "@@ -10,3 +10,5 @@ def run():\n"
        "+    x = compute()\n"
        "+    return x\n"
    )

    res = filter_review_diff(diff)
    assert "uv.lock" in res.skipped_files
    assert res.skipped_files["uv.lock"] == "lockfile"
    assert "tests/__snapshots__/out.snap" in res.skipped_files
    assert res.skipped_files["tests/__snapshots__/out.snap"] == "snapshot"
    assert "static/bundle.min.js" in res.skipped_files
    assert res.skipped_files["static/bundle.min.js"] == "minified"

    assert res.kept_files == ["src/core.py"]
    assert res.is_reviewable is True
    assert res.reviewable_lines == 2
    assert res.budget == 2
    assert "src/core.py" in res.filtered_diff
    assert "uv.lock" not in res.filtered_diff


def test_review_budget_round_decay_and_lifetime_cap():
    """Verify comment budget decay across rounds and lifetime cap suppression."""
    # Round 1
    allowance_r1 = compute_round_allowance(
        round_number=1,
        previous_round_posted=0,
        total_posted_so_far=0,
        initial_budget=5,
    )
    assert allowance_r1 == 5

    # Round 2: decays by 0.6 basis
    allowance_r2 = compute_round_allowance(
        round_number=2,
        previous_round_posted=5,
        total_posted_so_far=5,
        initial_budget=5,
    )
    assert allowance_r2 == 3

    # Round 3: further decays
    allowance_r3 = compute_round_allowance(
        round_number=3,
        previous_round_posted=3,
        total_posted_so_far=8,
        initial_budget=5,
    )
    assert allowance_r3 == 1

    # Lifetime cap reached (total >= 25)
    allowance_capped = compute_round_allowance(
        round_number=4,
        previous_round_posted=1,
        total_posted_so_far=25,
        lifetime_cap=25,
    )
    assert allowance_capped == 0
    assert should_suppress_round(25, lifetime_cap=25) is True
    assert should_suppress_round(24, lifetime_cap=25) is False

    # Review history summarizer
    mock_rev1 = MagicMock()
    mock_rev1.commit_id = "sha1"
    mock_rev2 = MagicMock()
    mock_rev2.commit_id = "sha2"

    mock_c1 = MagicMock()
    mock_c1.commit_id = "sha1"
    mock_c2 = MagicMock()
    mock_c2.commit_id = "sha2"

    history = summarise_review_history(
        bot_reviews=[mock_rev1, mock_rev2],
        bot_comments=[mock_c1, mock_c2],
    )
    assert history.round_number == 3
    assert history.last_reviewed_sha == "sha2"
    assert history.total_posted_so_far == 2
    assert history.previous_round_posted == 1


def test_duplicate_comment_suppression_and_repeat_grouping():
    """Verify duplicate comment suppression (proximity + token similarity) and 3+ repeat grouping."""
    existing_comments = [
        {
            "path": "src/module.py",
            "line": 42,
            "body": "[CRITICAL] Potential resource leak because file is never closed.",
        }
    ]
    zones, texts = build_exclusions(existing_comments)

    # Proximity match (line 43 is within ±2 of line 42 on same path)
    dup_reason_prox = already_raised(
        "src/module.py",
        43,
        "Another completely different issue here.",
        zones,
        texts,
    )
    assert "already commented near src/module.py:43" in dup_reason_prox

    # Similarity match (high Jaccard overlap on a different line/path)
    dup_reason_sim = already_raised(
        "src/other.py",
        100,
        "Resource leak detected because the opened file descriptor is never closed.",
        zones,
        texts,
    )
    assert "similar (" in dup_reason_sim

    # Novel comment is not suppressed
    no_dup = already_raised(
        "src/other.py",
        100,
        "Undefined variable usage on query parameter.",
        zones,
        texts,
    )
    assert no_dup == ""

    # Group repeated findings for 3+ occurrences
    issues = [
        IssueItem(
            path=f"src/file_{i}.py",
            line=10,
            description="Missing timeout on HTTP requests call",
            suggested_fix="requests.get(url, timeout=10)",
        )
        for i in range(4)
    ]
    grouped = group_repeated_findings(issues, threshold=3)
    assert len(grouped) == 1
    assert "Same thing in 3 other places in this review." in grouped[0].description


def test_build_github_review_comments_with_duplicate_and_budget():
    """Verify build_github_review_comments integrates duplicate suppression and round budgets."""
    diff = (
        "--- a/src/handler.py\n"
        "+++ b/src/handler.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+line1 = call_a()\n"
        "@@ -10,1 +10,1 @@\n"
        "+line10 = call_b()\n"
        "@@ -20,1 +20,1 @@\n"
        "+line20 = call_c()\n"
    )
    existing_comments = [
        {
            "path": "src/handler.py",
            "line": 1,
            "body": "Fix call_a syntax error",
        }
    ]

    issue1 = IssueItem(
        path="src/handler.py",
        line=1,
        description="Fix call_a syntax error",
    )
    issue2 = IssueItem(
        path="src/handler.py",
        line=10,
        description="Call_b needs error handling",
    )
    issue3 = IssueItem(
        path="src/handler.py",
        line=20,
        description="Call_c should be async",
    )

    # With duplicate suppression on issue1 and budget cap of 1
    comments, keys = build_github_review_comments(
        [issue1, issue2, issue3],
        diff,
        existing_comments=existing_comments,
        max_comments=1,
    )

    # issue1 is suppressed as duplicate of line 1
    # issue2 (line 10) is included
    # issue3 (line 20) is skipped because max_comments=1 was reached
    assert len(comments) == 1
    assert comments[0]["line"] == 10
    assert "src/handler.py:10" in keys
