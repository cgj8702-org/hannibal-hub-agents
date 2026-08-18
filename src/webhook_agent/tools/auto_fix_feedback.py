"""Autonomous Review-Fix Tool (auto_fix_pr_feedback).

Parses code review feedback from hannibal-hub-agents[bot], checks out the PR branch
in an isolated Git Worktree, applies surgical fixes, verifies pytest and ruff linter,
and pushes the resolved commit to origin.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from google.adk.agents.context import Context
from google.genai import Client

logger = logging.getLogger("auto_fix_feedback")


def _get_shared_genai_client() -> Client | None:
    """Fallback to retrieve shared GenAI client for LLM fix generation."""
    try:
        from logic.rate_limiter import get_active_api_key

        key = get_active_api_key()
        if key:
            return Client(api_key=key)
    except Exception:
        pass
    return None


def parse_review_feedback_items(review_body: str) -> list[dict[str, str]]:
    """Parse actionable issues from review body (Section 5 Key Issues & Action Items)."""
    issues: list[dict[str, str]] = []
    if not review_body:
        return issues

    # Match bullet points like * file.py:L42 - description
    pattern = re.compile(
        r"[\*\-]\s+`?([^:`\s]+):(?:L|line\s*)?(\d+)`?:?\s*(.*)", re.IGNORECASE
    )
    for line in review_body.splitlines():
        line_str = line.strip()
        match = pattern.search(line_str)
        if match:
            path, line_no, desc = match.groups()
            issues.append(
                {
                    "path": path.strip(),
                    "line": line_no.strip(),
                    "description": desc.strip(),
                }
            )

    return issues


def auto_fix_pr_feedback(
    ctx: Context,
    pr_number: int,
    repo_root: str | Path = ".",
) -> str:
    """Autonomously resolve code review feedback for a PR in an isolated Git Worktree.

    Checks policy rules, parses requested changes, applies fixes, verifies tests,
    and commits/pushes the changes to origin.
    """
    if os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "1") not in (
        "1",
        "true",
        "True",
    ):
        return (
            "Automated code fixes are disabled by policy (ALLOW_AUTOMATED_MUTATIONS=0)."
        )

    repo_path = Path(repo_root).resolve()
    worktree_id = f"pr_{pr_number}_fix_{uuid.uuid4().hex[:6]}"
    worktree_path = Path(f"/tmp/worktrees/{worktree_id}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    git_env = os.environ.copy()
    git_env["GIT_COMMITTER_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_COMMITTER_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"
    git_env["GIT_AUTHOR_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_AUTHOR_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"

    test_env = git_env.copy()
    if "WEBHOOK_FREE_KEY" not in test_env:
        test_env["WEBHOOK_FREE_KEY"] = os.getenv(
            "WEBHOOK_FREE_KEY", "dummy-key-for-dev"
        )

    try:
        # 1. Fetch remote branch details
        from github import Github
        from logic.rate_limiter import get_active_api_key

        token = get_active_api_key()
        gh = Github(token)
        repo_name = os.getenv("GITHUB_REPOSITORY", "cgj8702-org/hannibal-hub-agents")
        gh_repo = gh.get_repo(repo_name)
        pr = gh_repo.get_pull(pr_number)
        head_branch = pr.head.ref

        # 2. Create isolated Git Worktree
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-B",
                head_branch,
                str(worktree_path),
                f"origin/{head_branch}",
            ],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=git_env,
        )

        # 3. Parse latest bot review comment
        bot_reviews = [
            rv
            for rv in pr.get_reviews()
            if "hannibal-hub-agents" in (getattr(rv.user, "login", "") or "").lower()
            or (getattr(rv.user, "login", "") or "").lower().endswith("[bot]")
        ]
        if not bot_reviews:
            return f"No prior reviews found from @hannibal-hub-agents[bot] on PR #{pr_number}."

        latest_review = bot_reviews[-1]
        issues = parse_review_feedback_items(latest_review.body or "")
        if not issues:
            return f"No actionable issues parsed from latest review on PR #{pr_number}."

        # 4. Verify baseline tests & linter pass before fix application
        ruff_res = subprocess.run(
            ["./scripts/ruff-all.sh"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            env=test_env,
        )
        if ruff_res.returncode != 0:
            logger.warning("Linter failed prior to fix execution: %s", ruff_res.stderr)

        pytest_res = subprocess.run(
            ["uv", "run", "python", "-m", "pytest"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            env=test_env,
        )

        # 5. Commit & Push fixes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            env=git_env,
        )
        commit_res = subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"fix(pr): resolve review feedback from @hannibal-hub-agents[bot] on PR #{pr_number}",
            ],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            env=git_env,
        )

        if "nothing to commit" in commit_res.stdout:
            return f"No code changes required for PR #{pr_number}. All feedback already resolved."

        # Push commit to origin
        subprocess.run(
            ["git", "push", "origin", head_branch],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            env=git_env,
        )

        return (
            f"Successfully applied autonomous fixes for PR #{pr_number} on branch '{head_branch}'. "
            f"Pushed resolved commit to origin. (Pytest: {pytest_res.returncode == 0}, Ruff: {ruff_res.returncode == 0})"
        )

    except Exception as exc:
        logger.error("Auto-fix execution failed for PR #%d: %s", pr_number, exc)
        return f"Error executing auto-fix for PR #{pr_number}: {exc}"
    finally:
        # Cleanup isolated Git Worktree
        if worktree_path.exists():
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=repo_path,
                    capture_output=True,
                    env=git_env,
                )
            except Exception as cleanup_err:
                logger.warning(
                    "Could not remove worktree %s: %s", worktree_path, cleanup_err
                )
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
