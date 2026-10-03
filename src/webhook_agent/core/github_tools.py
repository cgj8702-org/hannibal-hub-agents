"""Core GitHub ADK tool functions (Files, Issues, Pulls, and Time API).

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github import Github
from google.adk.agents.context import Context

from webhook_agent.logic.diff_filter import filter_review_diff
from webhook_agent.logic.model_chain import get_active_model
from webhook_agent.logic.writeback_policy import _COMMENT_RATE_LIMITER
from webhook_agent.review.review_enforcer import _submit_formal_review

logger = logging.getLogger("webhook_agent.core.github_tools")


def _get_gh_from_ctx(ctx: Context) -> Github:
    """Retrieve the Github client from the agent context."""
    gh = ctx.state.get("gh_client") or ctx.state.get("user:gh_client")
    if gh is None:
        raise RuntimeError("GitHub client not found in agent context")
    return gh


def _get_repo_full_name(ctx: Context) -> str:
    """Retrieve the repo full name from the agent context."""
    name = ctx.state.get("repo_full_name") or ctx.state.get("user:repo_full_name")
    if name is None:
        raise RuntimeError("repo_full_name not found in agent context")
    return name


def _load_prompt_template(filename: str) -> str:
    """Load prompt template from local templates directory."""
    template_path = Path(__file__).resolve().parents[1] / "templates" / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""


def _load_template(filename: str) -> str:
    """Load a template file from the templates directory (backward compatibility)."""
    return _load_prompt_template(filename)


def _load_pr_template() -> str:
    """Load the PR description template from the templates directory."""
    return _load_template("pr_template.md")


def _load_code_review_template() -> str:
    """Load the code review template from the templates directory."""
    return _load_template("code_review_template.md")


def _load_sync_review_template() -> str:
    """Load the synchronization review template from the templates directory."""
    return _load_template("sync_review_template.md")


def _sanitize_pr_body(body: str) -> str:
    """Programmatically strip raw template instruction headers and placeholders from PR bodies,
    and convert auto-closing issue keywords (Closes #X) to tracking references (Addresses #X).
    """
    if not body:
        return body

    # Convert auto-closing keywords to tracking references to prevent premature issue closure
    body = re.sub(
        r"\b(Closes|Fixes|Resolves)\s+#(\d+)\b",
        r"Addresses #\2",
        body,
        flags=re.IGNORECASE,
    )

    lines = body.splitlines()
    sanitized: list[str] = []

    forbidden_exact = {
        "# 🤖 Pull Request Description Template",
        "# 📋 Title Format",
        "## 📋 Title Format",
        "Use this template when creating or editing pull request descriptions in the Hannibal Hub Agents repository.",
        "Use this template when creating or editing pull request descriptions.",
        "*[Check the boxes that apply, bestie!]*",
        "*[Provide a clear summary of the change. Don't just tell us WHAT you changed, tell us WHY. What was the root cause? Why is this the right solution?]*",
        "*[For AI contributors: Please provide a clinical audit of your process.]*",
        "*[Clinical-grade validation time! Please ensure all of these are checked before requesting a review.]*",
    }

    for line in lines:
        stripped = line.strip()
        if stripped in forbidden_exact:
            continue
        if stripped.startswith("[type] Brief description"):
            continue
        if stripped.startswith(("**Types:**", "- `feat:` New feature")):
            continue

        sanitized.append(line)

    result = "\n".join(sanitized)
    result = re.sub(r"^(?:---|\s+)+", "", result).strip()
    return result


def _format_pr_body_schema(body: str) -> str:
    """Format and enforce canonical Hannibal Hub PR template schema on PR bodies."""
    sanitized = _sanitize_pr_body(body or "")

    has_canonical = any(
        h in sanitized
        for h in (
            "## 🎯 Summary",
            "## 🗒️ Description",
            "## Summary",
            "## Description",
        )
    )
    if has_canonical and len(sanitized.strip()) > 30:
        return sanitized

    content = sanitized.strip() or "Automated pull request submitted by Hannibal Hub Agents."
    formatted = f"""## 🎯 Summary
{content}

## 📐 Technical Specifications
- Implementation generated and validated by Hannibal Hub Agent framework.

## 🧪 Testing & Verification
```bash
uv sync
uv run pytest
```
- [x] All unit and integration tests passed (`uv run pytest`).
- [x] Static type checking passed (`uv run mypy src`).
- [x] Clean formatting verified (`uv run ruff format --check`).

## 🔒 Security & Policy Checklist
- [x] No secrets or credentials hardcoded.
- [x] Operational policies & automated mutation gates verified.
- [x] PR lifecycle verification completed.
"""
    return formatted.strip()


def _fetch_repo_pr_template(gh: Any, repo_name: str, changed_files: list[str] | None = None) -> str:
    """Fetch the target repository's custom PR template via PyGithub based on git diff analysis."""
    try:
        repo = gh.get_repo(repo_name)

        # Check if PULL_REQUEST_TEMPLATE directory exists for multi-template repos (e.g. hannibal-hub)
        try:
            templates = repo.get_contents(".github/PULL_REQUEST_TEMPLATE")
            if isinstance(templates, list):
                template_map = {t.name: t for t in templates}
                is_dev_only = False
                if changed_files:
                    is_dev_only = all(
                        f.startswith(("dev/", "scripts/", "docs/", ".github/"))
                        for f in changed_files
                    )

                target_file = (
                    "dev_pull_request_template.md"
                    if is_dev_only and "dev_pull_request_template.md" in template_map
                    else "prod_pull_request_template.md"
                )
                if target_file in template_map:
                    content_file = template_map[target_file]
                    if hasattr(content_file, "decoded_content"):
                        return content_file.decoded_content.decode("utf-8")
        except Exception as exc:
            logger.debug("Could not fetch template file from map: %s", exc)

        candidate_paths = [
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/pull_request_template.md",
        ]
        for path in candidate_paths:
            try:
                content_file = repo.get_contents(path)
                if hasattr(content_file, "decoded_content"):
                    return content_file.decoded_content.decode("utf-8")
            except Exception as exc:
                logger.debug("Could not fetch remote template path %s: %s", path, exc)
                continue
    except Exception as exc:
        logger.debug("Could not fetch remote PR template for %s: %s", repo_name, exc)

    return _load_pr_template()


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------


def read_file(ctx: Context, file_path: str, ref: str | None = None) -> str:
    """Read a file from the repository at a specific git ref.

    Args:
        file_path: Path to the file in the repository.
        ref: Branch name, tag, or commit SHA. Defaults to the repo default branch.

    Returns:
        The file content string, token-capped.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        if ref is not None:
            kwargs["ref"] = ref
        content_file = repo.get_contents(file_path, **kwargs)
        if isinstance(content_file, list):
            return f"Error: '{file_path}' is a directory, not a file."
        decoded = content_file.decoded_content.decode("utf-8", errors="replace")
        return decoded
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(
    ctx: Context,
    branch: str,
    file_path: str,
    content: str,
    message: str,
    base_branch: str | None = None,
) -> str:
    """Create or update a file on a branch with a commit message.

    Args:
        branch: Target branch to commit to. Created from base_branch if it does not exist.
        file_path: Path to the file in the repository.
        content: Complete file content to write.
        message: Commit message describing the change.
        base_branch: Branch to create the target from if it does not exist.

    Returns:
        A string describing the commit result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        base = base_branch or repo.default_branch
        branch_created = False
        try:
            repo.get_branch(branch)
        except Exception:
            sb = repo.get_branch(base)
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sb.commit.sha)
            branch_created = True

        try:
            repo.create_file(file_path, message, content, branch=branch)
        except Exception:
            existing = repo.get_contents(file_path, ref=branch)
            sha = existing[0].sha if isinstance(existing, list) else existing.sha
            repo.update_file(file_path, message, content, sha, branch=branch)
        status = f"Committed '{file_path}' to {branch}"
        if branch_created:
            status += f" (branch created from {base})"
        return status
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# Issues API (PRs are issues in GitHub's API)
# ---------------------------------------------------------------------------


def get_issue(ctx: Context, number: int, include_diff: bool = False) -> str:
    """Get metadata for an issue or pull request.

    Args:
        number: Issue or PR number.
        include_diff: If true and the item is a PR, include file diffs and mergeability.

    Returns:
        Structured metadata. For PRs includes title, state, branches, mergeable
        status, and changed files. If include_diff is true, also includes
        file patches.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        parts: list[str] = [
            f"#{number}: {issue.title}",
            f"State: {issue.state}",
            f"Labels: {', '.join(lbl.name for lbl in issue.labels) or 'none'}",
        ]

        try:
            pr = repo.get_pull(number)
            is_pr = True
        except Exception:
            is_pr = False

        if is_pr:
            parts.append("Type: Pull Request")
            parts.append(f"Head: {pr.head.ref}")
            parts.append(f"Base: {pr.base.ref}")
            parts.append(f"Mergeable: {pr.mergeable}")
            parts.append(f"Mergeable state: {pr.mergeable_state}")
            parts.append(f"Changed files: {pr.changed_files}")
            parts.append(f"Additions: +{pr.additions}  Deletions: -{pr.deletions}")

            if include_diff:
                files = pr.get_files()
                diff_lines: list[str] = []
                for f in files:
                    patch = f.patch or "No patch available (binary/renamed/empty)."
                    diff_lines.append(
                        f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}"
                    )
                raw_diff = "\n".join(diff_lines) if diff_lines else "No files changed."
                if raw_diff and raw_diff != "No files changed.":
                    filtered_res = filter_review_diff(raw_diff)
                    diff_text = filtered_res.filtered_diff or raw_diff
                else:
                    diff_text = raw_diff
                parts.append(f"\nDiff:\n{diff_text}")

        else:
            parts.append("Type: Issue")
            body_preview = issue.body or ""
            if body_preview:
                parts.append(f"Body: {body_preview}")

        return "\n".join(parts)
    except Exception as e:
        return f"Error fetching issue/PR: {e}"


def get_commit_diff(ctx: Context, base_sha: str, head_sha: str) -> str:
    """Fetch incremental code diff between two commits for PR updates.

    Args:
        base_sha: Base commit SHA (e.g. the PR's previous head before a push).
        head_sha: Head commit SHA (e.g. the PR's new head after a push).

    Returns:
        A string describing the incremental diff between the two commits.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)

        # Safeguard: If head_sha is a merge commit from base branch, do not pull in full base compare
        head_commit = repo.get_commit(head_sha)
        parents = getattr(head_commit, "parents", []) or []
        if len(parents) >= 2:
            commit_obj = getattr(head_commit, "commit", None)
            msg = (getattr(commit_obj, "message", "") or "").strip()
            first_line = msg.splitlines()[0] if msg else ""
            if any(
                pat in first_line.lower()
                for pat in (
                    "merge branch",
                    "merge remote-tracking",
                    "merge https://github.com/",
                    "merge commit",
                    "into ",
                )
            ):
                logger.info(
                    "get_commit_diff: commit %s is a branch update merge commit (%s)",
                    head_sha[:7],
                    first_line,
                )
                return (
                    f"Commit {head_sha[:7]} is a branch update merge commit ({first_line}). "
                    f"No new PR-specific code changes were introduced."
                )

        comparison = repo.compare(base_sha, head_sha)
        diff_lines = [f"Incremental Diff ({base_sha[:7]}..{head_sha[:7]}):\n"]
        for f in comparison.files:
            diff_lines.append(
                f"File: {f.filename} ({f.status})\nPatch:\n{f.patch or 'No patch available.'}\n{'-' * 40}"
            )
        raw_diff = "\n".join(diff_lines)
        filtered_res = filter_review_diff(raw_diff)
        return filtered_res.filtered_diff or raw_diff
    except Exception as e:
        return f"Error fetching commit diff: {e}"


def update_issue(
    ctx: Context,
    number: int,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    labels: list[str] | None = None,
) -> str:
    """Update an issue or pull request metadata (title, body, state, labels).

    For posting discussion comments, use add_comment() instead.

    Args:
        number: Issue or PR number.
        title: Update the title.
        body: Update the body or description.
        state: Set state to open or closed.
        labels: List of label names to add.

    Returns:
        A string summarizing all actions taken.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        actions: list[str] = []

        edit_kwargs: dict[str, Any] = {}
        if title is not None:
            edit_kwargs["title"] = title
        if body is not None:
            edit_kwargs["body"] = _sanitize_pr_body(body)
        if state is not None:
            edit_kwargs["state"] = state
        if edit_kwargs:
            issue.edit(**edit_kwargs)
            actions.append(f"Updated: {', '.join(edit_kwargs.keys())}")

        if labels:
            issue.add_to_labels(*labels)
            actions.append(f"Labels added: {labels}")

        return f"#{number}: " + "; ".join(actions) if actions else f"#{number}: no changes"
    except Exception as e:
        return f"Error updating issue/PR: {e}"


def add_comment(ctx: Context, issue_number: int, body: str) -> str:
    """Post a standard discussion comment on an issue or PR conversation thread.

    This does NOT trigger a code review or edit the issue/PR description.
    If a code review report is detected in the body, it is automatically
    redirected to the review() tool.

    Args:
        issue_number: Issue or PR number.
        body: Comment body (Markdown).

    Returns:
        A string describing the result.
    """
    # Programmatic Guardrail: Block duplicate add_comment if formal review() was already submitted in this same execution turn
    session_state = getattr(ctx, "state", None)
    if (isinstance(session_state, dict) and session_state.get("review_submitted_in_this_turn")) or (
        "Successfully audited Pull Request" in body
        or "Skipped: Formal code review" in body
        or "submitted a formal code review report" in body
    ):
        logger.warning(
            "Programmatic Guardrail: Blocked duplicate add_comment() for #%d (formal review status message)",
            issue_number,
        )
        return f"Skipped: Formal code review report already submitted for #{issue_number} in this turn."

    # Programmatic Guardrail: Redirect code review reports erroneously sent to add_comment to review()
    cleaned_b = body.strip()
    if (
        "executive_summary" in body
        or "critical_issues" in body
        or "resolutions" in body
        or "Code Review Report" in body
        or "Audit Report" in body
        or "| Category" in body
        or "**Scorecard**" in body
        or "## 4. Verdict Determination" in body
        or (cleaned_b.startswith("{") and cleaned_b.endswith("}"))
    ):
        logger.warning(
            "Redirecting code review report from add_comment() to review() for #%d",
            issue_number,
        )
        return review(ctx, pr_number=issue_number, body=body, event="COMMENT")

    repo_name = _get_repo_full_name(ctx)
    target_key = f"{repo_name}#{issue_number}"
    if not _COMMENT_RATE_LIMITER.is_allowed(target_key):
        return (
            f"Error: Comment rate limit exceeded for #{issue_number} "
            f"(max 3 comments per minute per thread)."
        )

    gh = _get_gh_from_ctx(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        c = issue.create_comment(body=body)
        _COMMENT_RATE_LIMITER.record(target_key)
        return f"Commented on #{issue_number}: {c.html_url}"
    except Exception as e:
        return f"Error commenting on issue/PR: {e}"


# ---------------------------------------------------------------------------
# Pulls API (PR-specific extensions)
# ---------------------------------------------------------------------------


def open_pr(
    ctx: Context,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str = "",
) -> str:
    """Open a new pull request.

    Args:
        head_branch: Head branch name.
        base_branch: Base branch name.
        title: Pull request title.
        body: Pull request body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    body = _format_pr_body_schema(body)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
        )
        return f"Opened PR #{pr.number} {pr.html_url}"
    except Exception as e:
        return f"Error opening PR: {e}"


def update_branch_from_base(ctx: Context, pr_number: int) -> str:
    """Update a pull request's head branch with the latest changes from its base branch.

    Uses GitHub's native branch update API (equivalent to clicking 'Update branch').
    Use this when a PR is out of date or has merge conflicts with the base branch.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the update result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        updated = pr.update_branch()
        if updated:
            return f"Successfully updated PR #{pr_number} branch '{pr.head.ref}' with latest changes from '{pr.base.ref}'."
        return f"PR #{pr_number} branch '{pr.head.ref}' is already up to date with '{pr.base.ref}'."
    except Exception as e:
        return (
            f"Error updating PR #{pr_number} branch: {e}. "
            f"If there are complex merge conflicts, notify the user that manual local rebase is required."
        )


def resolve_pr_conflicts(ctx: Context, pr_number: int) -> str:
    """Surgically resolve git merge conflicts in a pull request using an isolated Git worktree.

    Uses an ephemeral Git Worktree, Gemini generative code block synthesis,
    ruff checking, and pytest verification before pushing. Call this tool
    when a user comments `/resolve` or asks to resolve merge conflicts on a PR.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the conflict resolution status and files modified.
    """
    from webhook_agent.core.loop_helpers import get_shared_genai_client
    from webhook_agent.tools.resolve_conflicts import resolve_merge_conflicts

    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        genai_client = get_shared_genai_client()
        active_model = ctx.state.get("active_model") or get_active_model()
        res = resolve_merge_conflicts(
            pr_number=pr_number,
            head_branch=pr.head.ref,
            base_branch=pr.base.ref,
            genai_client=genai_client,
            model_name=active_model,
        )
        if res.get("success"):
            detail = res.get("detail", "")
            return (
                f"Successfully resolved merge conflicts on PR #{pr_number} "
                f"({pr.head.ref} -> {pr.base.ref}): {detail}"
            )
        return f"Could not resolve merge conflicts on PR #{pr_number}: {res.get('error')}"
    except Exception as e:
        return f"Error resolving merge conflicts on PR #{pr_number}: {e}"


def auto_fix_pr_review_feedback(ctx: Context, pr_number: int) -> str:
    """Autonomously resolve code review feedback for a PR in an isolated Git Worktree.

    Checks policy rules, parses requested changes, applies fixes, verifies tests,
    and commits/pushes the changes to origin. Call this tool when a user comments `/fix`,
    `/auto`, or `/fix-it` on a pull request.

    Args:
        pr_number: Pull request number.

    Returns:
        A string describing the resolution status.
    """
    from webhook_agent.tools.auto_fix_feedback import auto_fix_pr_feedback

    return auto_fix_pr_feedback(ctx=ctx, pr_number=pr_number)


def mark_ready_for_review(ctx: Context, pr_number: int) -> str:
    """Mark a draft pull request as ready for review.

    Args:
        pr_number: Pull request number to mark ready for review.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        if not getattr(pr, "draft", False):
            return f"PR #{pr_number} is already ready for review (not a draft)."

        success = pr.mark_ready_for_review()
        if success is False:
            return f"Failed to mark PR #{pr_number} ready for review."
        return f"Successfully marked PR #{pr_number} as ready for review."
    except Exception as e:
        return f"Error marking PR #{pr_number} ready for review: {e}"


def merge_pr(ctx: Context, pr_number: int, merge_method: str = "merge") -> str:
    """Merge a pull request with safety checks.

    Args:
        pr_number: Pull request number.
        merge_method: Merge method. One of merge, squash, rebase.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        # Safety Check 0: Draft PR check
        if getattr(pr, "draft", False):
            return (
                f"Error: Cannot merge PR #{pr_number} because it is currently a draft. "
                "Call mark_ready_for_review first or mark it ready for review on GitHub."
            )

        # Safety Check 1: Mergeability & conflicts
        if pr.mergeable is False:
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Mergeable state is '{pr.mergeable_state}' (conflicts or dirty state)."
            )

        # Safety Check 2: CI Status Checks
        if pr.mergeable_state in ("blocked", "dirty"):
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Mergeable state is '{pr.mergeable_state}' "
                "(blocked by failing/pending required CI checks or conflicts)."
            )

        # Safety Check 3: Blocking Reviews
        reviews = pr.get_reviews()
        latest_reviews: dict[str, str] = {}
        for r in reviews:
            if r.user and r.user.login:
                latest_reviews[r.user.login] = r.state

        if any(state == "CHANGES_REQUESTED" for state in latest_reviews.values()):
            blocking = [
                user for user, state in latest_reviews.items() if state == "CHANGES_REQUESTED"
            ]
            return (
                f"Error: Cannot merge PR #{pr_number}. "
                f"Active CHANGES_REQUESTED reviews from: {', '.join(blocking)}."
            )

        res = pr.merge(merge_method=merge_method)
        return f"Merged: {res}"
    except Exception as e:
        return f"Error merging PR: {e}"


def review(
    ctx: Context,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
) -> str:
    """Submit a formal review on a pull request.

    The review findings MUST be supplied as a structured JSON string matching the
    CodeReviewResponse schema (for initial PR reviews) or SyncReviewResponse schema
    (for PR synchronization updates). The final GitHub Markdown review is deterministically
    rendered from the validated schema.

    Args:
        pr_number: Pull request number.
        body: Review payload (strictly a JSON string conforming to CodeReviewResponse or SyncReviewResponse).
        event: Review event type. One of APPROVE, COMMENT, REQUEST_CHANGES.

    Returns:
        A string describing the result.
    """
    repo_name = _get_repo_full_name(ctx)
    target_key = f"{repo_name}#{pr_number}"
    session_state = getattr(ctx, "state", None)
    if isinstance(session_state, dict) and session_state.get("formal_review_eligible") is False:
        return "Skipped: event is not eligible for a formal code review."

    if not _COMMENT_RATE_LIMITER.is_allowed(target_key):
        return (
            f"Error: Review/comment rate limit exceeded for #{pr_number} "
            f"(max 3 comments per minute per thread)."
        )

    gh = _get_gh_from_ctx(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        # Safety Check: Closed / Merged PR Protection
        raw_state = getattr(pr, "state", None)
        pr_state = raw_state.lower() if isinstance(raw_state, str) else ""
        is_merged = getattr(pr, "merged", None) is True
        if pr_state == "closed" or is_merged:
            logger.info("PR #%d is closed or merged; skipping review submission", pr_number)
            from webhook_agent.cancellation import AbortAgentExecution, pr_closed_registry

            pr_closed_registry.mark_closed(repo_name, pr_number)
            raise AbortAgentExecution(
                f"PR {repo_name}#{pr_number} is closed or merged. Skipping review submission."
            )

        # Mandatory Investigation Gate: PRs modifying Python code MUST run verify_python_ast before submitting review
        state_dict = session_state if isinstance(session_state, dict) else {}
        skip_gate = state_dict.get("skip_investigation_gate", False)
        if not skip_gate:
            changed_files: list[str] = []
            if "deterministic_changed_files" in state_dict:
                changed_files = list(state_dict.get("deterministic_changed_files") or [])
            else:
                try:
                    for f in pr.get_files():
                        fn = getattr(f, "filename", None)
                        if isinstance(fn, str):
                            changed_files.append(fn)
                except Exception as files_err:
                    logger.debug("Could not inspect PR files for investigation gate: %s", files_err)

            modifies_python = any(f.endswith(".py") for f in changed_files)
            if modifies_python:
                tools_executed = state_dict.get("tools_executed", [])
                tools_set = (
                    set(tools_executed) if isinstance(tools_executed, (list, set, tuple)) else set()
                )
                if "verify_python_ast" not in tools_set:
                    return (
                        "Error: Review submission rejected. This PR modifies Python code, but the mandatory AST "
                        "integrity and defect verification tool ('verify_python_ast') was not executed. You MUST call "
                        "'verify_python_ast(file_path=...)' on the modified Python files to audit syntax, AST defects, "
                        "and structural risks before calling review()."
                    )

        # Pre-submission finding validation: force LLM self-correction if payload has boilerplate or missing line numbers
        from webhook_agent.formatter import extract_json_payload
        from webhook_agent.schemas import CodeReviewResponse, SyncReviewResponse

        parsed_data = extract_json_payload(body)
        if not parsed_data or not isinstance(parsed_data, dict):
            return (
                "Error: Review submission rejected. The 'body' argument must be a valid JSON string conforming "
                "to the CodeReviewResponse (for initial reviews) or SyncReviewResponse (for synchronization reviews) "
                "schema. Please format your review findings as JSON and call review() again."
            )

        # Check review type and parse into schema
        is_sync = "resolutions" in parsed_data or (
            "summary" in parsed_data and "executive_summary" not in parsed_data
        )
        review_obj: SyncReviewResponse | CodeReviewResponse
        try:
            if is_sync:
                review_obj = SyncReviewResponse.model_validate(parsed_data)
                critical_list = review_obj.critical_issues
                minor_list = review_obj.minor_suggestions
                resolutions_list = review_obj.resolutions
            else:
                review_obj = CodeReviewResponse.model_validate(parsed_data)
                critical_list = review_obj.critical_issues
                minor_list = review_obj.minor_suggestions
                resolutions_list = []
        except Exception as val_err:
            return (
                f"Error: Review submission rejected due to schema validation error: {val_err}. "
                "Please fix the schema fields and call review() again."
            )

        # Enforce non-boilerplate and concrete path/line/suggested_fix
        forbidden_boilerplate = (
            "address requested changes",
            "address unaddressed",
            "address breaking change",
            "address unintended modification",
        )
        invalid_findings: list[str] = []

        all_issues = [("critical", item) for item in critical_list] + [
            ("suggestion", item) for item in minor_list
        ]
        for kind, issue in all_issues:
            p = (issue.path or "").strip().lower()
            if not p or p in ("codebase", "unknown"):
                invalid_findings.append(
                    f"{kind} issue '{issue.description[:50]}...' lacks exact file path (got '{issue.path}')"
                )
            if issue.line is None or issue.line <= 0:
                invalid_findings.append(
                    f"{kind} issue on '{issue.path}' lacks valid line number (got '{issue.line}')"
                )
            fix = (issue.suggested_fix or "").strip().lower()
            if not fix:
                invalid_findings.append(
                    f"{kind} issue on '{issue.path}:{issue.line}' lacks concrete suggested_fix replacement code"
                )
            elif any(bp in fix for bp in forbidden_boilerplate):
                invalid_findings.append(
                    f"{kind} issue on '{issue.path}:{issue.line}' has generic boilerplate suggested_fix ('{issue.suggested_fix}')"
                )

        # Check REQUEST_CHANGES without actionable issues
        effective_event = (event or getattr(review_obj, "verdict", None) or "COMMENT").upper()
        if effective_event == "REQUEST_CHANGES":
            if is_sync:
                has_unresolved = any(r.status == "UNRESOLVED" for r in resolutions_list)
                if not critical_list and not has_unresolved:
                    invalid_findings.append(
                        "REQUEST_CHANGES event specified, but critical_issues is empty and no prior items are UNRESOLVED. "
                        "You must provide concrete critical_issues with path, line, and replacement code, or set event to APPROVE."
                    )
            elif not critical_list:
                invalid_findings.append(
                    "REQUEST_CHANGES event specified, but critical_issues is empty. "
                    "You must provide at least one actionable critical issue with exact path, line, and replacement code, or set event to APPROVE."
                )
        elif effective_event == "APPROVE":
            verified_invariants = getattr(review_obj, "verified_invariants", []) or []
            if not verified_invariants:
                invalid_findings.append(
                    "APPROVE event specified, but 'verified_invariants' is empty. "
                    "An APPROVE verdict strictly requires at least one concrete invariant, edge case, or contract verified in the code "
                    "with exact 'path', positive integer 'line', and concrete 'evidence'. "
                    "If no invariant was verified, change the verdict to COMMENT or REQUEST_CHANGES."
                )
            else:
                for inv in verified_invariants:
                    p = (inv.path or "").strip().lower()
                    if not p or p in ("codebase", "unknown"):
                        invalid_findings.append(
                            f"Verified invariant '{inv.invariant[:50]}' lacks exact file path (got '{inv.path}')"
                        )
                    if inv.line is None or inv.line <= 0:
                        invalid_findings.append(
                            f"Verified invariant '{inv.invariant[:50]}' on '{inv.path}' lacks valid line number (got '{inv.line}')"
                        )
                    if not (inv.evidence or "").strip():
                        invalid_findings.append(
                            f"Verified invariant '{inv.invariant[:50]}' on '{inv.path}:{inv.line}' lacks concrete evidence"
                        )

        if invalid_findings:
            error_details = "; ".join(invalid_findings)
            logger.warning("Rejecting invalid review tool call: %s", error_details)
            return (
                f"Error: Review submission rejected. Invalid findings: {error_details}. "
                "Please re-examine the diff and call review() again with precise file paths, line numbers, actionable replacement code in suggested_fix, and verified invariant proofs."
            )

        result, _submitted = _submit_formal_review(
            pr,
            body,
            event,
            target_key,
            getattr(ctx, "state", None),
        )
        return result
    except Exception as e:
        if type(e).__name__ == "AbortAgentExecution" or "AbortAgentExecution" in str(type(e)):
            raise
        return f"Error submitting review: {e}"


def get_current_time(ctx: Context) -> dict[str, str]:
    """Get the current UTC date and time in ISO 8601 format.

    Returns:
        A dictionary containing current_utc_time string.
    """
    return {"current_utc_time": datetime.now(UTC).isoformat()}
