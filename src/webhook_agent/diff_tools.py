"""Diff-grounding and AST line verification FunctionTools for Webhook Agent."""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools import FunctionTool


def parse_unified_diff(diff_text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse a unified git diff text into a file map of modified hunks and line ranges."""
    file_map: dict[str, list[dict[str, Any]]] = {}
    if not diff_text:
        return file_map

    current_file: str | None = None
    file_diff_pattern = re.compile(r"^diff --git a/(.*?) b/(.*)")
    hunk_pattern = re.compile(r"^@@ -\d+,\d+ \+(\d+),(\d+) @@")

    for line in diff_text.splitlines():
        match_file = file_diff_pattern.match(line)
        if match_file:
            current_file = match_file.group(2)
            file_map[current_file] = []
            continue

        if current_file and line.startswith("@@"):
            match_hunk = hunk_pattern.match(line)
            if match_hunk:
                start_line = int(match_hunk.group(1))
                line_count = int(match_hunk.group(2))
                end_line = start_line + max(0, line_count - 1)
                file_map[current_file].append(
                    {
                        "start_line": start_line,
                        "end_line": end_line,
                        "hunk_header": line,
                    }
                )

    return file_map


def get_pr_diff_file_map(diff_text: str) -> dict[str, Any]:
    """Return file paths, modified hunk line ranges, and line counts from unified diff text."""
    parsed = parse_unified_diff(diff_text)
    summary: dict[str, Any] = {
        "modified_files": list(parsed.keys()),
        "file_hunks": parsed,
    }
    return summary


def verify_line_reference(diff_text: str, file_path: str, line_number: int) -> bool:
    """Verify if a cited line number falls within any modified diff hunk for the given file."""
    parsed = parse_unified_diff(diff_text)
    if file_path not in parsed:
        return False

    for hunk in parsed[file_path]:
        if hunk["start_line"] <= line_number <= hunk["end_line"]:
            return True

    return False


get_pr_diff_file_map_tool = FunctionTool(get_pr_diff_file_map)
verify_line_reference_tool = FunctionTool(verify_line_reference)
