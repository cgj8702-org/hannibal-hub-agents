"""Diff-grounding and AST line verification FunctionTools for Webhook Agent.

Includes diff hunk anchor extraction logic adapted directly from adk-samples/.github/scripts/post_review_comments.py.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools import FunctionTool

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_diff_prefix(path: str) -> str:
    """Drop git's `a/`/`b/` diff prefix from a path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def added_line_anchors(diff: str) -> dict[str, set[int]]:
    """Map each path to the new-file line numbers this diff adds or modifies.

    Adapted directly from adk-samples/.github/scripts/post_review_comments.py.
    """
    anchors: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    old_remaining = 0
    new_remaining = 0

    for row in diff.splitlines():
        if old_remaining <= 0 and new_remaining <= 0:
            if row.startswith("+++ "):
                target = row[4:].strip()
                path = None if target == "/dev/null" else _strip_diff_prefix(target)
                continue
            header = HUNK_HEADER.match(row)
            if header:
                old_remaining = int(header.group(2) or 1)
                new_line = int(header.group(3))
                new_remaining = int(header.group(4) or 1)
            continue

        if row.startswith("\\"):
            continue
        if row.startswith("+"):
            if path is not None:
                anchors.setdefault(path, set()).add(new_line)
            new_line += 1
            new_remaining -= 1
        elif row.startswith("-"):
            old_remaining -= 1
        else:
            new_line += 1
            new_remaining -= 1
            old_remaining -= 1

    return anchors


def get_pr_diff_file_map(diff_text: str) -> dict[str, Any]:
    """Return file paths, modified hunk line ranges, and line counts from unified diff text."""
    anchors = added_line_anchors(diff_text)
    summary: dict[str, Any] = {
        "modified_files": list(anchors.keys()),
        "anchors": {k: sorted(list(v)) for k, v in anchors.items()},
    }
    return summary


def verify_line_reference(diff_text: str, file_path: str, line_number: int) -> bool:
    """Verify if a cited line number falls within any modified diff hunk for the given file."""
    stripped_path = _strip_diff_prefix(file_path)
    anchors = added_line_anchors(diff_text)

    if file_path in anchors and line_number in anchors[file_path]:
        return True
    if stripped_path in anchors and line_number in anchors[stripped_path]:
        return True

    return False


get_pr_diff_file_map_tool = FunctionTool(get_pr_diff_file_map)
verify_line_reference_tool = FunctionTool(verify_line_reference)
