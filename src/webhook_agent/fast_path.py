"""Fast-path evaluation and base branch merge detection for PR webhooks.

Extracted from processor.py as part of Phase 3d modularization.
"""

from __future__ import annotations

import logging
from typing import Any

from github import Github

from webhook_agent.review.review_enforcer import _submit_formal_review

logger = logging.getLogger("webhook_agent.fast_path")


def is_base_branch_merge_sync(gh: Github, repo_name: str, payload: dict[str, Any]) -> bool:
    """Check if a pull_request.synchronize event is an update from the base branch (e.g. merging main).

    When a PR branch is updated with the base branch (via GitHub's 'Update branch' button
    or `git merge main`), the head commit is a merge commit from the base branch.
    Such events should NOT trigger automated re-reviews or dismiss existing approvals.
    """
    try:
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict):
            return False

        canonical = payload.get("canonical", "")
        action = raw.get("action")
        if canonical != "pull_request.synchronize" and action != "synchronize":
            return False

        pr_data = raw.get("pull_request")
        if not isinstance(pr_data, dict):
            return False

        head = pr_data.get("head") or {}
        head_sha = head.get("sha") or raw.get("after")
        if not head_sha:
            return False

        base = pr_data.get("base") or {}
        base_ref = (base.get("ref") or "").lower()
        base_sha = base.get("sha") or ""
        head_ref = (head.get("ref") or "").lower()
        before_sha = raw.get("before") or ""

        # Fast-path: Check raw_payload commits if available
        commits = raw.get("commits")
        if isinstance(commits, list) and commits:
            for c in commits:
                if isinstance(c, dict) and c.get("id") == head_sha:
                    msg = (c.get("message") or "").strip().lower()
                    first_line = msg.splitlines()[0] if msg else ""
                    if any(
                        pat in first_line
                        for pat in (
                            f"merge branch '{base_ref}'",
                            f"merge branch '{base_ref}' of",
                            f"merge remote-tracking branch 'origin/{base_ref}'",
                            "merge https://github.com/",
                            f"into {head_ref}",
                        )
                    ):
                        return True

        repo = gh.get_repo(repo_name)
        commit = repo.get_commit(head_sha)
        parents = getattr(commit, "parents", []) or []
        if len(parents) < 2:
            return False

        commit_obj = getattr(commit, "commit", None)
        msg = (getattr(commit_obj, "message", "") or "").strip().lower()
        first_line = msg.splitlines()[0] if msg else ""

        merge_patterns = (
            f"merge branch '{base_ref}'",
            f"merge branch '{base_ref}' of",
            f"merge remote-tracking branch 'origin/{base_ref}'",
            "merge https://github.com/",
            "merge commit",
            f"into {head_ref}",
        )
        is_merge_msg = any(pat in first_line for pat in merge_patterns)

        parent_shas = [p.sha for p in parents if hasattr(p, "sha")]
        is_base_parent = base_sha in parent_shas or is_merge_msg

        return bool(
            is_merge_msg or (is_base_parent and (not before_sha or before_sha in parent_shas))
        )
    except Exception as exc:
        logger.debug("Could not verify base branch merge sync: %s", exc)
        return False


def evaluate_dependency_fast_path(
    gh: Github,
    repo_name: str,
    payload: dict[str, Any],
    raw: dict[str, Any],
    pr_data: dict[str, Any],
    pr_number: Any,
    dry_run: bool = False,
) -> bool:
    """Evaluate deterministic fast-path for automated Dependabot / lockfile PRs.

    Returns:
        True if deterministically approved and handled; False to fall back to agent.
    """
    if dry_run or pr_number is None:
        return False

    try:
        from webhook_agent.analysis.lockfile_validator import (
            is_pure_dependency_pr,
            render_deterministic_approval_markdown,
            validate_lockfile_diff,
        )

        sender_login = (
            (payload.get("sender") or {}).get("login", "")
            or (raw.get("sender") or {}).get("login", "")
            or ""
        )
        head_branch = (pr_data.get("head") or {}).get("ref", "")
        changed_files = raw.get("changed_files") or []
        pr_diff = raw.get("pr_diff") or ""

        if is_pure_dependency_pr(sender_login, head_branch, changed_files):
            logger.info(
                "⚡ Evaluating deterministic fast-path for PR %s#%s (%d changed files)",
                repo_name,
                pr_number,
                len(changed_files),
            )
            val_res = validate_lockfile_diff(pr_diff, changed_files)
            if val_res.should_approve:
                pr_num_int = int(pr_number)
                repo = gh.get_repo(repo_name)
                pr = repo.get_pull(pr_num_int)
                approval_body = render_deterministic_approval_markdown(val_res, pr_num_int)
                target_key = f"{repo_name}#{pr_number}"
                status_msg, submitted = _submit_formal_review(
                    pr=pr,
                    body=approval_body,
                    event="APPROVE",
                    target_key=target_key,
                )
                logger.info(
                    "⚡ Fast-Path: Deterministically approved dependency PR %s#%s: %s (submitted=%s)",
                    repo_name,
                    pr_number,
                    status_msg,
                    submitted,
                )
                return True
            else:
                logger.info(
                    "⚡ Fast-Path: Dependency PR %s#%s bypassed to full agent audit: %s (%s)",
                    repo_name,
                    pr_number,
                    val_res.summary,
                    val_res.rejection_reason,
                )
    except Exception as fast_path_err:
        logger.warning(
            "Deterministic fast-path evaluation encountered error, falling back to agent: %s",
            fast_path_err,
        )

    return False


__all__ = [
    "evaluate_dependency_fast_path",
    "is_base_branch_merge_sync",
]
