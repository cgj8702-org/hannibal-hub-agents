"""Codebase search tool for Webhook Agent.

Allows the auditor agent to search the codebase using pure Python filesystem traversal,
verifying call sites, checking imports, and inspecting variable usages without external binaries.
"""

from __future__ import annotations

import logging
import os
import re

from google.adk.agents.context import Context
from google.adk.tools import FunctionTool

logger = logging.getLogger("webhook_agent.codebase_search")

MAX_SEARCH_RESULTS = 20
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".build",
    "dist",
    "build",
}


def search_codebase(
    ctx: Context,
    query: str,
    file_pattern: str | None = None,
) -> str:
    """Search the codebase using exact text patterns or regex to find call sites, definitions, or usages.

    Use this tool to:
    - Search for function definitions, class names, imports, or variable usages.
    - Check if environment variables have defaults across secondary scripts or tests.
    - Answer questions about existing repo conventions BEFORE outputting review comments.

    Args:
        ctx: ADK execution context.
        query: Search string or regex pattern to look for in the repository.
        file_pattern: Optional glob pattern (e.g., "*.py", "*.sh", "*.md") to filter files.

    Returns:
        Formatted matching lines (path:line:content) or notice if no matches found.
    """
    if not query or not query.strip():
        return "Error: Empty search query provided."

    repo_dir = os.getcwd()
    q = query.strip()
    try:
        query_regex = re.compile(q)
    except re.error:
        query_regex = None

    pattern_clean = (
        file_pattern.strip().lstrip("*") if file_pattern and file_pattern.strip() else None
    )

    output_lines: list[str] = []

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for f in files:
            if pattern_clean and not f.endswith(pattern_clean):
                continue
            path = os.path.join(root, f)
            rel_path = os.path.relpath(path, repo_dir)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for idx, line in enumerate(fh, 1):
                        matched = (q in line) or (
                            query_regex is not None and query_regex.search(line) is not None
                        )
                        if matched:
                            output_lines.append(f"`{rel_path}:{idx}`: {line.rstrip()}")
                            if len(output_lines) >= MAX_SEARCH_RESULTS:
                                break
            except Exception:
                pass
            if len(output_lines) >= MAX_SEARCH_RESULTS:
                break

    if not output_lines:
        return f"No matches found in codebase for query: '{q}'."

    count_str = (
        f"Showing top {len(output_lines)} matches"
        if len(output_lines) >= MAX_SEARCH_RESULTS
        else f"Found {len(output_lines)} matches"
    )
    return f"### 🔍 Codebase Search Results for '{q}' ({count_str}):\n\n" + "\n".join(
        f"* {item}" for item in output_lines
    )


search_codebase_tool = FunctionTool(search_codebase)
