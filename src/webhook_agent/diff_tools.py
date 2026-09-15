"""Backward-compatibility shim for webhook_agent.diff_tools.

Re-exports from webhook_agent.tools.diff_tools.
"""

from .tools.diff_tools import (
    HUNK_HEADER,
    MAX_DRIFT,
    WINDOW_LINE,
    _snap_to_window,
    _strip_diff_prefix,
    _window_rows,
    added_line_anchors,
    check_window,
    get_pr_diff_file_map,
    get_pr_diff_file_map_tool,
    verify_line_reference,
    verify_line_reference_tool,
    walk_right_side,
)

__all__ = [
    "HUNK_HEADER",
    "MAX_DRIFT",
    "WINDOW_LINE",
    "_snap_to_window",
    "_strip_diff_prefix",
    "_window_rows",
    "added_line_anchors",
    "check_window",
    "get_pr_diff_file_map",
    "get_pr_diff_file_map_tool",
    "verify_line_reference",
    "verify_line_reference_tool",
    "walk_right_side",
]
