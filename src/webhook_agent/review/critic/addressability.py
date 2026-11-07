"""Diff right-side addressability and comment placement verification.

Adapted from google/adk-samples/.github/scripts/post_review_comments.py.
Verifies whether a line number falls within diff hunk right-side additions/modifications.
Findings on added lines can be posted as inline review comments. Findings on context lines
or outside the diff are routed to top-level review notes to avoid GitHub review submission errors.
"""

from __future__ import annotations

from collections.abc import Collection

from webhook_agent.review.schemas import IssueItem
from webhook_agent.tools.diff_tools import walk_right_side


class AddressabilityChecker:
    """Verifies whether findings are addressable within diff hunk right-side boundaries."""

    def __init__(self, diff_text: str = "") -> None:
        self.diff_text = diff_text
        self.anchors, self.line_text = walk_right_side(diff_text) if diff_text else ({}, {})

    def update_diff(self, diff_text: str) -> None:
        """Update active diff and recompute anchors and line text."""
        self.diff_text = diff_text
        self.anchors, self.line_text = walk_right_side(diff_text) if diff_text else ({}, {})

    def is_inline_addressable(self, file_path: str, line_number: int | None) -> bool:
        """Return True if (file_path, line_number) is an added/modified right-side diff line."""
        if not file_path or line_number is None or line_number <= 0:
            return False

        path = file_path[2:] if file_path.startswith(("a/", "b/")) else file_path
        if path in self.anchors and line_number in self.anchors[path]:
            return True

        # Check unstripped path
        return bool(file_path in self.anchors and line_number in self.anchors[file_path])

    def partition_addressability(
        self, issues: Collection[IssueItem]
    ) -> tuple[list[IssueItem], list[IssueItem]]:
        """Partition findings into inline-addressable and top-level notes.

        Returns:
            (inline_issues, top_level_issues)
        """
        inline_issues: list[IssueItem] = []
        top_level_issues: list[IssueItem] = []

        for issue in issues:
            if self.is_inline_addressable(issue.path, issue.line):
                inline_issues.append(issue)
            else:
                top_level_issues.append(issue)

        return inline_issues, top_level_issues

    def get_file_lines(self, file_path: str) -> dict[int, str]:
        """Return mapped line text for a given path from right-side diff hunks."""
        path = file_path[2:] if file_path.startswith(("a/", "b/")) else file_path
        if path in self.line_text:
            return self.line_text[path]
        return self.line_text.get(file_path, {})
