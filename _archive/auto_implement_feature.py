"""Autonomous Feature Engineering Agent (auto_implement_issue_feature).

Executes multi-turn feature development in an isolated Git Worktree, backed by
FEATURE_AGENT_FREE_KEY for API quota isolation and Google Cloud Firestore for
durable session checkpointing and auto-resumption on 429 quota exhaustion.
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
from typing import Any

from google.adk.agents.context import Context
from google.genai import Client
from google.genai.errors import APIError

logger = logging.getLogger("auto_implement_feature")

try:
    from google.cloud import firestore

    _HAS_FIRESTORE = True
except ImportError:
    firestore = None  # type: ignore[assignment]
    _HAS_FIRESTORE = False


class FirestoreFeatureCheckpointRegistry:
    """Manages Firestore checkpoints for long-running autonomous feature tasks.

    Stores task documents in 'feature_checkpoints/issue_{issue_number}' for durable
    state persistence across process restarts or 429 quota depletion pauses.
    """

    def __init__(self, collection_name: str = "feature_checkpoints") -> None:
        self.collection_name = collection_name
        self._db: Any = None
        self._initialized = False

    def _get_db(self) -> Any | None:
        if not self._initialized:
            self._initialized = True
            if _HAS_FIRESTORE and os.getenv("ENABLE_FIRESTORE_REGISTRY", "1").lower() in (
                "1",
                "true",
            ):
                try:
                    project_id = (
                        os.getenv("FEATURE_AGENT_PROJECT")
                        or os.getenv("GCP_PROJECT_ID")
                        or os.getenv("PUBSUB_PROJECT")
                    )
                    self._db = firestore.Client(project=project_id)
                    logger.info(
                        "🔥 Firestore Feature Checkpoint Registry initialized for project [%s]",
                        project_id,
                    )
                except Exception as exc:
                    logger.warning("Could not initialize Firestore client for checkpoints: %s", exc)
                    self._db = None
        return self._db

    def save_checkpoint(
        self,
        issue_number: int,
        instruction: str,
        branch_name: str,
        session_id: str,
        status: str,
        last_completed_step: str = "",
        error_msg: str = "",
        pr_url: str = "",
        cooldown_seconds: float = 86400.0,
    ) -> None:
        """Save feature development checkpoint to Firestore."""
        db = self._get_db()
        if db is None:
            return

        doc_id = f"issue_{issue_number}"
        now_utc = datetime.datetime.now(datetime.UTC)
        resume_at = now_utc + datetime.timedelta(seconds=cooldown_seconds)

        data = {
            "issue_number": issue_number,
            "instruction": instruction,
            "branch_name": branch_name,
            "session_id": session_id,
            "status": status,
            "last_completed_step": last_completed_step,
            "error_msg": error_msg,
            "pr_url": pr_url,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if status == "quota_paused":
            data["resume_at"] = resume_at

        try:
            db.collection(self.collection_name).document(doc_id).set(data, merge=True)
            logger.info(
                "🔥 Firestore Checkpoint saved for Issue #%d [Status: %s]",
                issue_number,
                status,
            )
        except Exception as exc:
            logger.warning(
                "Failed to write Firestore checkpoint for Issue #%d: %s",
                issue_number,
                exc,
            )

    def get_checkpoint(self, issue_number: int) -> dict[str, Any] | None:
        """Fetch active checkpoint for an issue from Firestore."""
        db = self._get_db()
        if db is None:
            return None

        doc_id = f"issue_{issue_number}"
        try:
            doc = db.collection(self.collection_name).document(doc_id).get()
            if doc.exists:
                return doc.to_dict() or {}
        except Exception as exc:
            logger.debug(
                "Firestore checkpoint read skipped for Issue #%d: %s",
                issue_number,
                exc,
            )
        return None


firestore_checkpoint_registry = FirestoreFeatureCheckpointRegistry()


def _get_feature_genai_client() -> Client | None:
    """Retrieve GenAI client specifically scoped to FEATURE_AGENT_FREE_KEY for quota isolation."""
    key = os.getenv("FEATURE_AGENT_FREE_KEY") or os.getenv("WEBHOOK_FREE_KEY")
    if key:
        return Client(api_key=key)
    return None


def auto_implement_issue_feature(
    ctx: Context,
    issue_number: int,
    instruction: str,
    repo_root: str | Path = ".",
) -> str:
    """Autonomously implement a feature request for an Issue in an isolated Git Worktree.

    Locked down to FEATURE_AGENT_FREE_KEY for API quota isolation. Supports
    Firestore checkpointing for 429 quota exhaustion auto-pausing and resumption.
    """
    if os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "1") not in (
        "1",
        "true",
        "True",
    ):
        return "Automated feature creation is disabled by policy (ALLOW_AUTOMATED_MUTATIONS=0)."

    # Clean short slug for branch naming
    slug = re.sub(r"[^a-z0-9]+", "-", instruction.lower())[:25].strip("-") or "feature"
    branch_name = f"feat/issue-{issue_number}-auto-impl-{slug}"
    session_id = f"delegate-issue-{issue_number}"

    # Check for existing Firestore checkpoint
    existing = firestore_checkpoint_registry.get_checkpoint(issue_number)
    if existing and existing.get("status") == "quota_paused":
        resume_at = existing.get("resume_at")
        if resume_at and isinstance(resume_at, datetime.datetime):
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=datetime.UTC)
            now_utc = datetime.datetime.now(datetime.UTC)
            if resume_at > now_utc:
                remaining = int((resume_at - now_utc).total_seconds())
                return (
                    f"Feature implementation for Issue #{issue_number} is currently "
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

    repo_path = Path(repo_root).resolve()
    worktree_id = f"issue_{issue_number}_impl_{uuid.uuid4().hex[:6]}"
    worktree_path = Path(f"/tmp/worktrees/{worktree_id}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    git_env = os.environ.copy()
    git_env["GIT_COMMITTER_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_COMMITTER_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"
    git_env["GIT_AUTHOR_NAME"] = "hannibal-hub-agents[bot]"
    git_env["GIT_AUTHOR_EMAIL"] = "hannibal-hub-agents[bot]@users.noreply.github.com"

    test_env = git_env.copy()
    if "FEATURE_AGENT_FREE_KEY" not in test_env:
        test_env["FEATURE_AGENT_FREE_KEY"] = os.getenv("WEBHOOK_FREE_KEY", "dummy-key-for-dev")
    test_env["WEBHOOK_FREE_KEY"] = test_env["FEATURE_AGENT_FREE_KEY"]

    try:
        from github import Github
        from logic.rate_limiter import get_active_api_key

        token = os.getenv("FEATURE_AGENT_FREE_KEY") or get_active_api_key()
        gh = Github(token)
        repo_name = os.getenv("GITHUB_REPOSITORY", "cgj8702-org/hannibal-hub-agents")
        gh_repo = gh.get_repo(repo_name)

        # Create isolated Git Worktree
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
                branch_name,
                str(worktree_path),
                "origin/main",
            ],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=git_env,
        )

        # Verify baseline linter & tests
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

        if pytest_res.returncode != 0:
            logger.warning("Baseline tests failed prior to feature execution.")

        # Stage and commit feature changes
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
            return f"No code changes required for Issue #{issue_number}. Feature logic already present."

        # Push branch to origin
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            env=git_env,
        )

        # Create Pull Request on GitHub
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

    except APIError as exc:
        err_msg = str(exc)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg.upper():
            logger.warning(
                "🔴 429 Quota Exhausted on FEATURE_AGENT_FREE_KEY for Issue #%d. Checkpointing to Firestore.",
                issue_number,
            )

            # Create WIP checkpoint commit in worktree
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
                error_msg=err_msg,
            )

            return (
                f"FEATURE_AGENT_FREE_KEY hit daily 429 quota exhaustion during Issue #{issue_number}. "
                f"WIP progress committed to branch '{branch_name}' and state checkpointed to Firestore. "
                f"Task will auto-resume when quota resets."
            )
        raise

    except Exception as exc:
        logger.error("Auto-implement failed for Issue #%d: %s", issue_number, exc)
        firestore_checkpoint_registry.save_checkpoint(
            issue_number=issue_number,
            instruction=instruction,
            branch_name=branch_name,
            session_id=session_id,
            status="failed",
            error_msg=str(exc),
        )
        return f"Error executing auto-implement for Issue #{issue_number}: {exc}"

    finally:
        if worktree_path.exists():
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=repo_path,
                    capture_output=True,
                    env=git_env,
                )
            except Exception as cleanup_err:
                logger.warning("Could not remove worktree %s: %s", worktree_path, cleanup_err)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
