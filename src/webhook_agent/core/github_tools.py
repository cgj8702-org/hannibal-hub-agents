"""Transitional backwards-compatibility shim for github_tools.

Relocated to webhook_agent.tools.github_tools in Phase 5 modularization.
"""

from __future__ import annotations

from webhook_agent.tools.github_tools import (
    add_label,
    create_issue,
    get_commit_diff,
    get_current_time,
    get_issue,
    read_file,
    review,
)

__all__ = [
    "add_label",
    "create_issue",
    "get_commit_diff",
    "get_current_time",
    "get_issue",
    "read_file",
    "review",
]
