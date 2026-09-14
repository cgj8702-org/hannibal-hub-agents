"""Diff-grounding and AST line verification FunctionTools for Webhook Agent.

Includes diff hunk anchor extraction logic adapted directly from adk-samples/.github/scripts/post_review_comments.py.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from google.adk.tools import FunctionTool

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
WINDOW_LINE = re.compile(r"^\s*(\d{1,12})\s*:\s*(.*)$")
ESCAPE_SEQ = re.compile(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\x[0-9a-fA-F]{2}")
MIN_PREFIX = 6
MAX_DRIFT = 5


def _strip_diff_prefix(path: str) -> str:
    """Drop git's `a/`/`b/` diff prefix from a path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _norm(text: str) -> str:
    """Reduce a source line to a comparable ASCII skeleton."""
    text = ESCAPE_SEQ.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c.isascii() and not c.isspace())


def walk_right_side(
    diff: str,
) -> tuple[dict[str, set[int]], dict[str, dict[int, str]]]:
    """Map each path to:
      1) The new-file line numbers this diff adds or modifies (anchors).
      2) The text of every right-side line shown in the diff (added or context).

    Adapted directly from adk-samples/.github/scripts/post_review_comments.py.
    """
    anchors: dict[str, set[int]] = {}
    text: dict[str, dict[int, str]] = {}
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
                text.setdefault(path, {})[new_line] = row[1:]
            new_line += 1
            new_remaining -= 1
        elif row.startswith("-"):
            old_remaining -= 1
        else:
            if path is not None:
                text.setdefault(path, {})[new_line] = row[1:]
            new_line += 1
            new_remaining -= 1
            old_remaining -= 1

    return anchors, text


def added_line_anchors(diff: str) -> dict[str, set[int]]:
    """Map each path to the new-file line numbers this diff adds or modifies."""
    return walk_right_side(diff)[0]


def _window_rows(window: object, default_line: int | None = None) -> list[tuple[int, str]]:
    """Extract [(line_number, text)] from a window string or code block."""
    rows: list[tuple[int, str]] = []
    raw_lines = str(window or "").splitlines()
    for raw in raw_lines:
        match = WINDOW_LINE.match(raw)
        if match:
            rows.append(
                (
                    int(match.group(1)),
                    re.sub(r"(\.{3}|\u2026)\s*$", "", match.group(2)),
                )
            )

    # If lines didn't carry explicit "line: text" prefixes, infer line numbering from default_line
    if not rows and default_line is not None:
        for idx, raw in enumerate(raw_lines):
            clean = raw.strip()
            if clean:
                rows.append((default_line + idx, clean))

    return rows


def _window_matches(rows: list[tuple[int, str]], lines: dict[int, str], offset: int) -> int:
    """How many window rows match the diff at this offset; 0 if any conflicts."""
    matched = 0
    for number, claimed in rows:
        actual = lines.get(number + offset)
        if actual is None:
            continue
        want, have = _norm(claimed), _norm(actual)
        if not want:
            continue
        if want == have or (
            min(len(want), len(have)) >= MIN_PREFIX
            and (want.startswith(have) or have.startswith(want))
        ):
            matched += 1
        else:
            return 0
    return matched


def _snap_to_window(line: int, rows: list[tuple[int, str]], offset: int) -> int:
    """Pull an anchor that sits outside its own verified window back into it."""
    claimed = [number + offset for number, _text in rows]
    if line in claimed:
        return line
    return min(claimed, key=lambda candidate: (abs(candidate - line), candidate))


def check_window(
    window_or_code: object,
    line: int,
    lines: dict[int, str],
    max_drift: int = MAX_DRIFT,
) -> tuple[bool, int, str]:
    """Verify quoted window rows against the diff text and correct line drift if needed.

    Returns:
        (verified, snapped_line, reason)
    """
    rows = _window_rows(window_or_code, line)
    if not rows:
        return False, line, "no window supplied"

    if _window_matches(rows, lines, 0):
        snapped = _snap_to_window(line, rows, 0)
        if snapped != line:
            return True, snapped, f"anchor {line} was outside its own window; moved to {snapped}"
        return True, line, "window matches the diff"

    for step in range(1, max_drift + 1):
        for offset in (step, -step):
            if _window_matches(rows, lines, offset) >= 1:
                return (
                    True,
                    _snap_to_window(line + offset, rows, offset),
                    f"window matched {offset:+d} lines away; anchor corrected",
                )

    number, claimed = rows[0]
    actual = lines.get(number, "")
    return False, line, f"window says {claimed.strip()[:40]!r}, diff has {actual.strip()[:40]!r}"


def get_pr_diff_file_map(diff_text: str) -> dict[str, Any]:
    """Return file paths, modified hunk line ranges, and line counts from unified diff text."""
    anchors = added_line_anchors(diff_text)
    summary: dict[str, Any] = {
        "modified_files": list(anchors.keys()),
        "anchors": {k: sorted(v) for k, v in anchors.items()},
    }
    return summary


def verify_line_reference(diff_text: str, file_path: str, line_number: int) -> bool:
    """Verify if a cited line number falls within any modified diff hunk for the given file."""
    stripped_path = _strip_diff_prefix(file_path)
    anchors = added_line_anchors(diff_text)

    if file_path in anchors and line_number in anchors[file_path]:
        return True
    return bool(stripped_path in anchors and line_number in anchors[stripped_path])


get_pr_diff_file_map_tool = FunctionTool(get_pr_diff_file_map)
verify_line_reference_tool = FunctionTool(verify_line_reference)
