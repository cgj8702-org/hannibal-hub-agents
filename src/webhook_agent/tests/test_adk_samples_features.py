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

from webhook_agent.comment_poster import build_github_review_comments
from webhook_agent.diff_tools import check_window, walk_right_side
from webhook_agent.formatter import (
    extract_json_payload,
    is_implausible_body,
    is_not_cheap_finding,
    normalize_code_review_dict,
)
from webhook_agent.schemas import IssueItem
from webhook_agent.webhook_agent import _enforce_verdict


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
