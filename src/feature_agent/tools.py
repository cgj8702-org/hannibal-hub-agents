"""Dedicated sandbox software engineering tools for feature_agent.

Provides file inspection, surgical edits, unit testing, linter validation,
git commit/push, and GitHub Pull Request creation inside Git Worktrees.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from google.adk.agents.context import Context

logger = logging.getLogger("feature_agent.tools")


def get_worktree_path(ctx: Context) -> Path:
    """Extract or fallback to active Git Worktree path from ADK Context state."""
    raw = getattr(ctx, "state", {}) or {}
    wt = raw.get("worktree_path")
    if wt and Path(wt).exists():
        return Path(wt).resolve()
    return Path(".").resolve()


def resolve_in_window(file_path: str, worktree_path: Path) -> Path:
    """Workspace Focus Lens: Enforce path boundary protection to prevent traversal."""
    resolved = (worktree_path / file_path).resolve()
    wt_str = str(worktree_path.resolve())
    if not str(resolved).startswith(wt_str):
        raise PermissionError(
            f"Path traversal blocked: '{file_path}' resolves outside target workspace window '{wt_str}'"
        )
    return resolved


def search_codebase(ctx: Context, query: str) -> str:
    """Search the codebase for a text or symbol pattern using ripgrep or git grep."""
    wt = get_worktree_path(ctx)
    try:
        res = subprocess.run(
            ["git", "grep", "-n", "-i", query],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = res.stdout.strip().splitlines()[:30]
            return f"Found {len(lines)} matches:\n" + "\n".join(lines)
        return f"No matches found for query '{query}'."
    except Exception as exc:
        return f"Codebase search error: {exc}"


def view_file(ctx: Context, file_path: str) -> str:
    """View contents of a file inside the active sandbox worktree."""
    wt = get_worktree_path(ctx)
    target = (wt / file_path).resolve()
    if not target.exists():
        return f"Error: File '{file_path}' does not exist."
    try:
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) > 500:
            return f"Showing first 500 lines of {file_path}:\n" + "\n".join(lines[:500])
        return text
    except Exception as exc:
        return f"Error reading file '{file_path}': {exc}"


def replace_file_content(
    ctx: Context,
    file_path: str,
    target_content: str,
    replacement_content: str,
) -> str:
    """Surgically replace target_content with replacement_content in a file."""
    wt = get_worktree_path(ctx)
    target = (wt / file_path).resolve()
    if not target.exists():
        return f"Error: File '{file_path}' does not exist."
    try:
        content = target.read_text(encoding="utf-8")
        if target_content not in content:
            return f"Error: target_content not found in '{file_path}'."
        updated = content.replace(target_content, replacement_content, 1)
        target.write_text(updated, encoding="utf-8")
        return f"Successfully updated '{file_path}'."
    except Exception as exc:
        return f"Error modifying file '{file_path}': {exc}"


def run_pytest(ctx: Context) -> str:
    """Run pytest suite inside the active sandbox worktree."""
    wt = get_worktree_path(ctx)
    env = os.environ.copy()
    key = os.getenv("FEATURE_AGENT_FREE_KEY") or os.getenv(
        "WEBHOOK_FREE_KEY", "dummy-key-for-dev"
    )
    env["WEBHOOK_FREE_KEY"] = key
    env["FEATURE_AGENT_FREE_KEY"] = key

    try:
        res = subprocess.run(
            ["uv", "run", "python", "-m", "pytest"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if res.returncode == 0:
            return "✅ pytest passed 100% cleanly!"
        return f"🔴 pytest failed (exit code {res.returncode}):\n{res.stdout}\n{res.stderr}"
    except Exception as exc:
        return f"Pytest execution error: {exc}"


def run_linter(ctx: Context) -> str:
    """Run ruff linter & formatter script inside the active sandbox worktree."""
    wt = get_worktree_path(ctx)
    try:
        res = subprocess.run(
            ["./scripts/ruff-all.sh"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            return "✅ Linter & formatter passed cleanly!"
        return f"🔴 Linter failed:\n{res.stdout}\n{res.stderr}"
    except Exception as exc:
        return f"Linter execution error: {exc}"


def commit_and_push(ctx: Context, commit_message: str) -> str:
    """Stage, commit, and push changes to origin for the active branch."""
    wt = get_worktree_path(ctx)
    git_env = os.environ.copy()
    git_env["GIT_COMMITTER_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_COMMITTER_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"
    git_env["GIT_AUTHOR_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_AUTHOR_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=wt,
            check=True,
            capture_output=True,
            env=git_env,
        )
        res = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=wt,
            capture_output=True,
            text=True,
            env=git_env,
        )
        if "nothing to commit" in res.stdout:
            return "No changes to commit."

        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt,
            capture_output=True,
            text=True,
            check=True,
        )
        branch_name = branch_res.stdout.strip()

        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=wt,
            check=True,
            capture_output=True,
            env=git_env,
        )
        return f"Successfully committed and pushed branch '{branch_name}' to origin."
    except Exception as exc:
        return f"Git commit/push error: {exc}"
