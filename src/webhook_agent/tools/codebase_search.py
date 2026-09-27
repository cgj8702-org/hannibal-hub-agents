"""Codebase search tool for Webhook Agent.

Allows the auditor agent to search the codebase to answer its own questions, verify call sites,
check imports, and inspect environment variable usages before generating final reviews.
"""

from __future__ import annotations

import logging
import os
import subprocess

from google.adk.agents.context import Context
from google.adk.tools import FunctionTool

logger = logging.getLogger("webhook_agent.codebase_search")

MAX_SEARCH_RESULTS = 20


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

    cmd = ["rg", "--no-heading", "--line-number", "--color=never", "--max-count=20"]
    if file_pattern and file_pattern.strip():
        cmd.extend(["-g", file_pattern.strip()])

    cmd.extend([query.strip(), repo_dir])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        output_lines = res.stdout.splitlines() if res.stdout else []
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as exc:
        logger.warning("ripgrep search failed (%s), falling back to python search", exc)
        output_lines = []

    if not output_lines:
        # Fallback to simple python grep if rg wasn't installed or failed
        output_lines = []
        q = query.strip()
        for root, _dirs, files in os.walk(repo_dir):
            if ".git" in root or "__pycache__" in root or ".venv" in root:
                continue
            for f in files:
                if file_pattern and not f.endswith(file_pattern.lstrip("*")):
                    continue
                path = os.path.join(root, f)
                rel_path = os.path.relpath(path, repo_dir)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        for idx, line in enumerate(fh, 1):
                            if q in line:
                                output_lines.append(f"{rel_path}:{idx}:{line.rstrip()}")
                                if len(output_lines) >= MAX_SEARCH_RESULTS:
                                    break
                except Exception:
                    pass
                if len(output_lines) >= MAX_SEARCH_RESULTS:
                    break

    if not output_lines:
        return f"No matches found in codebase for query: '{query.strip()}'."

    truncated = output_lines[:MAX_SEARCH_RESULTS]
    formatted = []
    for entry in truncated:
        parts = entry.split(":", 2)
        if len(parts) == 3:
            rel_path = os.path.relpath(parts[0], repo_dir)
            formatted.append(f"`{rel_path}:{parts[1]}`: {parts[2].strip()}")
        else:
            formatted.append(entry)

    count_str = (
        f"Showing top {len(formatted)} matches"
        if len(output_lines) >= MAX_SEARCH_RESULTS
        else f"Found {len(formatted)} matches"
    )
    return f"### 🔍 Codebase Search Results for '{query.strip()}' ({count_str}):\n\n" + "\n".join(
        f"* {item}" for item in formatted
    )


search_codebase_tool = FunctionTool(search_codebase)
