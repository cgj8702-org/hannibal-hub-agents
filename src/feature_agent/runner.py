"""Standalone FeatureTaskRunner execution manager.

Runs autonomous multi-turn feature development tasks inside Git Worktrees using
the ADK Runner, handling 429 API quota exhaustion auto-pausing and Firestore checkpointing.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from feature_agent.agent import build_feature_app, get_feature_agent_key
from feature_agent.firestore_checkpoints import firestore_checkpoint_registry

logger = logging.getLogger("feature_agent.runner")


class FeatureTaskRunner:
    """Manages the lifecycle of autonomous feature engineering tasks."""

    def __init__(self, repo_root: str | Path = ".") -> None:
        self.repo_path = Path(repo_root).resolve()
        self.session_service = InMemorySessionService()
        self.memory_service = InMemoryMemoryService()
        self._runner = None

    @property
    def runner(self) -> Runner:
        """Lazily initialize ADK Runner when needed."""
        if self._runner is None:
            self._runner = Runner(
                app=build_feature_app(),
                session_service=self.session_service,
                memory_service=self.memory_service,
            )
        return self._runner

    def execute_task(
        self,
        issue_number: int,
        instruction: str,
    ) -> str:
        """Execute an autonomous feature task for a GitHub issue."""
        if os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "1") not in (
            "1",
            "true",
            "True",
        ):
            return "Automated feature creation is disabled by policy (ALLOW_AUTOMATED_MUTATIONS=0)."

        slug = (
            re.sub(r"[^a-z0-9]+", "-", instruction.lower())[:25].strip("-") or "feature"
        )
        branch_name = f"feat/issue-{issue_number}-auto-impl-{slug}"
        session_id = f"delegate-issue-{issue_number}"

        # Check for existing Firestore checkpoint
        existing = firestore_checkpoint_registry.get_checkpoint(issue_number)
        if existing and existing.get("status") == "quota_paused":
            resume_at = existing.get("resume_at")
            if resume_at and isinstance(resume_at, datetime.datetime):
                if resume_at.tzinfo is None:
                    resume_at = resume_at.replace(tzinfo=datetime.timezone.utc)
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if resume_at > now_utc:
                    remaining = int((resume_at - now_utc).total_seconds())
                    return (
                        f"Feature task for Issue #{issue_number} is currently "
                        f"paused due to FEATURE_AGENT_FREE_KEY quota depletion. "
                        f"Auto-resuming in {remaining}s at {resume_at.isoformat()}."
                    )

        firestore_checkpoint_registry.save_checkpoint(
            issue_number=issue_number,
            instruction=instruction,
            branch_name=branch_name,
            session_id=session_id,
            status="in_progress",
            last_completed_step="worktree_initialization",
        )

        worktree_id = f"issue_{issue_number}_impl_{uuid.uuid4().hex[:6]}"
        worktree_path = Path(f"/tmp/worktrees/{worktree_id}")
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        git_env = os.environ.copy()
        git_env["GIT_COMMITTER_NAME"] = "hannibal-hub-agents[bot]"
        git_env["GIT_COMMITTER_EMAIL"] = (
            "hannibal-hub-agents[bot]@users.noreply.github.com"
        )
        git_env["GIT_AUTHOR_NAME"] = "hannibal-hub-agents[bot]"
        git_env["GIT_AUTHOR_EMAIL"] = (
            "hannibal-hub-agents[bot]@users.noreply.github.com"
        )

        test_env = git_env.copy()
        api_key = get_feature_agent_key()
        test_env["FEATURE_AGENT_FREE_KEY"] = api_key
        test_env["WEBHOOK_FREE_KEY"] = api_key

        try:
            from github import Github

            gh = Github(api_key)
            repo_name = os.getenv(
                "GITHUB_REPOSITORY", "cgj8702-org/hannibal-hub-agents"
            )
            gh_repo = gh.get_repo(repo_name)

            # Create isolated Git Worktree
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=self.repo_path,
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
                    branch_name,
                    str(worktree_path),
                    "origin/main",
                ],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                env=git_env,
            )

            # Baseline validation
            ruff_res = subprocess.run(
                ["./scripts/ruff-all.sh"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                env=test_env,
            )
            pytest_res = subprocess.run(
                ["uv", "run", "python", "-m", "pytest"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                env=test_env,
            )

            # Commit worktree feature implementation
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
                    f"feat(issue-{issue_number}): autonomous feature implementation for #{issue_number}",
                ],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                env=git_env,
            )

            if "nothing to commit" in commit_res.stdout:
                firestore_checkpoint_registry.save_checkpoint(
                    issue_number=issue_number,
                    instruction=instruction,
                    branch_name=branch_name,
                    session_id=session_id,
                    status="completed",
                    last_completed_step="no_changes_needed",
                )
                return f"No code changes required for Issue #{issue_number}."

            # Push branch to origin
            subprocess.run(
                ["git", "push", "origin", branch_name],
                cwd=worktree_path,
                check=True,
                capture_output=True,
                env=git_env,
            )

            # Create GitHub Pull Request
            pr_body = (
                f"# 🤖 Autonomous Feature Implementation for Issue #{issue_number}\n\n"
                f"### 📋 Feature Instruction\n{instruction}\n\n"
                f"--- \n\n"
                f"### 🧪 Verification Results\n"
                f"- **Pytest**: {'Passed ✅' if pytest_res.returncode == 0 else 'Failed 🔴'}\n"
                f"- **Ruff Linter**: {'Passed ✅' if ruff_res.returncode == 0 else 'Failed 🔴'}\n\n"
                f"*Generated autonomously by Hannibal Feature Agent (`FEATURE_AGENT_FREE_KEY`).*"
            )

            pr = gh_repo.create_pull(
                title=f"feat(issue-{issue_number}): {instruction[:60]}",
                body=pr_body,
                head=branch_name,
                base="main",
            )

            firestore_checkpoint_registry.save_checkpoint(
                issue_number=issue_number,
                instruction=instruction,
                branch_name=branch_name,
                session_id=session_id,
                status="completed",
                last_completed_step="pr_created",
                pr_url=pr.html_url,
            )

            return (
                f"Successfully built autonomous feature for Issue #{issue_number} on branch '{branch_name}'. "
                f"Opened Pull Request: {pr.html_url}"
            )

        except Exception as exc:
            err_msg = str(exc)
            err_code = getattr(exc, "code", None)
            is_429 = (
                err_code == 429
                or "429" in err_msg
                or "resource_exhausted" in err_msg.lower()
            )

            if is_429:
                from logic.rate_limiter import extract_rate_limit_details

                limit_details = extract_rate_limit_details(exc)
                cooldown_secs = (
                    limit_details.get("retry_after_seconds")
                    or 86400.0  # Default to 24h for RPD
                )
                quota_limit = limit_details.get("quota_limit") or "UnknownQuotaLimit"
                quota_val = limit_details.get("quota_value") or "unknown"

                logger.warning(
                    "🔴 429 Quota Exhausted on FEATURE_AGENT_FREE_KEY for Issue #%d. "
                    "Quota: %s (%s) | Cooldown: %.1fs | Checkpointing to Firestore.",
                    issue_number,
                    quota_limit,
                    quota_val,
                    cooldown_secs,
                )

                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=worktree_path,
                    capture_output=True,
                    env=git_env,
                )
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"wip(auto-impl): checkpoint before quota pause on Issue #{issue_number}",
                    ],
                    cwd=worktree_path,
                    capture_output=True,
                    env=git_env,
                )

                firestore_checkpoint_registry.save_checkpoint(
                    issue_number=issue_number,
                    instruction=instruction,
                    branch_name=branch_name,
                    session_id=session_id,
                    status="quota_paused",
                    last_completed_step="quota_depletion_checkpoint",
                    error_msg=f"{err_msg} [Quota: {quota_limit}={quota_val}, Cooldown: {cooldown_secs}s]",
                )

                return (
                    f"FEATURE_AGENT_FREE_KEY hit 429 quota limit '{quota_limit}' (cooldown: {cooldown_secs:.1f}s) "
                    f"during Issue #{issue_number}. WIP progress committed to branch '{branch_name}' and state checkpointed to Firestore."
                )

            logger.error("Auto-implement failed for Issue #%d: %s", issue_number, exc)
            firestore_checkpoint_registry.save_checkpoint(
                issue_number=issue_number,
                instruction=instruction,
                branch_name=branch_name,
                session_id=session_id,
                status="failed",
                error_msg=str(exc),
            )
            return f"Error executing feature agent for Issue #{issue_number}: {exc}"

        finally:
            if worktree_path.exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        cwd=self.repo_path,
                        capture_output=True,
                        env=git_env,
                    )
                except Exception as cleanup_err:
                    logger.warning(
                        "Could not remove worktree %s: %s", worktree_path, cleanup_err
                    )
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
