"""Core GitHub ADK tool functions (Files, Issues, Pulls, and Time API).

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from github import Github
from google.adk.agents.context import Context

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER

logger = logging.getLogger("webhook_agent.tools.github_tools")


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


def _extract_state_dict(session_state: Any) -> dict[str, Any]:
    """Safely convert ADK State or dict to a standard Python dictionary."""
    if session_state is None:
        return {}
    if hasattr(session_state, "to_dict"):
        try:
            return session_state.to_dict()
        except Exception:
            pass
    if isinstance(session_state, dict):
        return session_state
    if hasattr(session_state, "get") and hasattr(session_state, "__iter__"):
        try:
            return {k: session_state[k] for k in session_state}
        except Exception:
            pass
    return {}


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


def add_label(ctx: Context, issue_number: int, labels: list[str]) -> str:
    """Add one or more labels to an issue or pull request (e.g. attaching 'jules' label).

    Args:
        issue_number: Issue or PR number.
        labels: List of label names to attach (e.g. ['jules']).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        issue.add_to_labels(*labels)
        return f"Successfully added labels {labels} to #{issue_number}."
    except Exception as e:
        return f"Error adding labels to #{issue_number}: {e}"


def create_issue(
    ctx: Context,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str:
    """Create a new GitHub issue in the repository.

    Use this tool to autonomously spawn well-defined tasks, bug reports,
    or refactoring jobs (e.g. attaching the 'jules' label so that Google Labs
    Jules can execute the implementation).

    Args:
        title: Issue title.
        body: Markdown issue description and structured task specification.
        labels: Optional list of label names to attach (e.g. ['jules']).

    Returns:
        A string describing the result, including issue number and URL.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {"title": title, "body": body}
        if labels:
            kwargs["labels"] = labels
        issue = repo.create_issue(**kwargs)
        labels_str = f" with labels {labels}" if labels else ""
        return f"Successfully created issue #{issue.number} ({getattr(issue, 'html_url', 'created')}){labels_str}."
    except Exception as e:
        return f"Error creating issue: {e}"


# ---------------------------------------------------------------------------
# Pulls API (PR-specific extensions)
# ---------------------------------------------------------------------------


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
    state_dict = _extract_state_dict(getattr(ctx, "state", None))
    if state_dict.get("formal_review_eligible") is False:
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
            from webhook_agent.core.cancellation import (
                AbortAgentExecution,
                pr_closed_registry,
            )

            pr_closed_registry.mark_closed(repo_name, pr_number)
            raise AbortAgentExecution(
                f"PR {repo_name}#{pr_number} is closed or merged. Skipping review submission."
            )

        # Mandatory Investigation Gate: PRs modifying Python code MUST run verify_python_ast before submitting review
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
                deterministic_precompiled = bool(state_dict.get("deterministic_precompiled_ast"))
                tools_executed = state_dict.get("tools_executed", [])
                tools_set = (
                    set(tools_executed) if isinstance(tools_executed, (list, set, tuple)) else set()
                )
                if not deterministic_precompiled and "verify_python_ast" not in tools_set:
                    return (
                        "Error: Review submission rejected. This PR modifies Python code, but the mandatory AST "
                        "integrity and defect verification tool ('verify_python_ast') was not executed. You MUST call "
                        "'verify_python_ast(file_path=...)' on the modified Python files to audit syntax, AST defects, "
                        "and structural risks before calling review()."
                    )

        # Pre-submission finding validation: force LLM self-correction if payload has boilerplate or missing line numbers
        from webhook_agent.review.formatter import extract_json_payload
        from webhook_agent.review.schemas import CodeReviewResponse, SyncReviewResponse

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
        if _submitted:
            inv = getattr(ctx, "_invocation_context", None)
            if inv is not None:
                inv.end_invocation = True
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
