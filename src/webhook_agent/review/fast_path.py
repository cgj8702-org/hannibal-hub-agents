"""Fast-path evaluation and base branch merge detection for PR webhooks.

Extracted from processor.py as part of Phase 3d modularization.
"""

from __future__ import annotations

import logging
from typing import Any

from github import Github

from webhook_agent.review.review_enforcer import _submit_formal_review

logger = logging.getLogger("webhook_agent.review.fast_path")


def is_base_branch_merge_sync(gh: Github, repo_name: str, payload: dict[str, Any]) -> bool:
    """Check if a pull_request.synchronize event is an authentic update from the base branch (e.g. merging main).

    Returns True only if all verification checks pass via GitHub API:
    1. Head commit has >= 2 parents.
    2. One parent equals before_sha, and the other (base_parent) satisfies
       repo.compare(base_ref, base_parent).status in ("behind", "identical").
    3. Every file changed between before_sha and head_sha (capped at 100 files) matches the exact blob sha
       of repo.get_contents(path, ref=base_parent).
    Fails closed (returns False) on any error, missing field, or cap overflow.
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
        base_ref = base.get("ref") or ""
        before_sha = raw.get("before") or ""

        if not before_sha or not base_ref:
            return False

        repo = gh.get_repo(repo_name)
        commit = repo.get_commit(head_sha)
        parents = getattr(commit, "parents", []) or []
        if len(parents) < 2:
            return False

        parent_shas = [getattr(p, "sha", str(p)) for p in parents]
        if before_sha not in parent_shas:
            return False

        other_parents = [sha for sha in parent_shas if sha != before_sha]
        if len(other_parents) != 1:
            return False
        base_parent = other_parents[0]

        # Verify base_parent is contained in base_ref (behind or identical)
        base_compare = repo.compare(base_ref, base_parent)
        if getattr(base_compare, "status", None) not in ("behind", "identical"):
            return False

        # Verify files changed between before_sha and head_sha match base_parent exactly
        diff_compare = repo.compare(before_sha, head_sha)
        diff_files = getattr(diff_compare, "files", None)
        if diff_files is None or len(diff_files) > 100:
            return False

        for f in diff_files:
            file_path = getattr(f, "filename", None)
            head_blob_sha = getattr(f, "sha", None)
            if not file_path or not head_blob_sha:
                return False
            base_content = repo.get_contents(file_path, ref=base_parent)
            base_blob_sha = getattr(base_content, "sha", None)
            if not base_blob_sha or base_blob_sha != head_blob_sha:
                return False

        logger.info(
            "Verified base-branch merge sync for %s (head=%s, base_parent=%s, files=%d)",
            repo_name,
            head_sha[:8],
            base_parent[:8],
            len(diff_files),
        )
        return True
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
