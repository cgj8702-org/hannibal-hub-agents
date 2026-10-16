"""Webhook Agent tools package.

Uses lazy imports via __getattr__ to decouple lightweight AST, diff, and search
tools from heavy external dependencies like PyGithub and Google ADK.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "TOOL_INPUT_SCHEMAS",
    "GetIssueInput",
    "ReadFileInput",
    "ReviewInput",
    "check_window",
    "get_issue",
    "read_file",
    "review",
    "search_files",
    "verify_line_reference",
    "verify_python_ast",
    "walk_right_side",
]


def __getattr__(name: str) -> Any:
    if name in {
        "TOOL_INPUT_SCHEMAS",
        "GetIssueInput",
        "ReadFileInput",
        "ReviewInput",
    }:
        mod = importlib.import_module(".schemas", __package__)
        return getattr(mod, name)

    if name in {"get_issue", "read_file", "review"}:
        mod = importlib.import_module(".github_tools", __package__)
        return getattr(mod, name)

    if name == "verify_python_ast":
        mod = importlib.import_module(".ast_tools", __package__)
        return getattr(mod, name)

    if name in {"check_window", "verify_line_reference", "walk_right_side"}:
        mod = importlib.import_module(".diff_tools", __package__)
        return getattr(mod, name)

    if name == "search_files":
        mod = importlib.import_module(".search_tools", __package__)
        return getattr(mod, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
