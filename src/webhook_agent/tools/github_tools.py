"""Core GitHub ADK tool functions (Files, Issues, and Pulls API).

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from github import Github
from google.adk.agents.context import Context

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.review.grounding import (
    extract_grounding_findings,
    strict_grounding_enabled,
)
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER

if TYPE_CHECKING:
    pass

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
        except Exception as err:
            logger.debug("session_state.to_dict() failed: %s", err)
    if isinstance(session_state, dict):
        return session_state
    if hasattr(session_state, "get") and hasattr(session_state, "__iter__"):
        try:
            return {k: session_state[k] for k in session_state}
        except Exception as err:
            logger.debug("Extracting session_state mapping failed: %s", err)
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
    clean_path = file_path.strip().strip("/")
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        if ref is not None:
            kwargs["ref"] = ref

        candidate_paths = [clean_path]
        if not clean_path.startswith("src/"):
            candidate_paths.append(f"src/{clean_path}")
        else:
            candidate_paths.append(clean_path[4:])

        content_file = None
        last_exc: Exception | None = None
        for p in candidate_paths:
            try:
                content_file = repo.get_contents(p, **kwargs)
                break
            except Exception as exc:
                last_exc = exc
                continue

        if content_file is None:
            return f"Error reading file: {last_exc}"

        if isinstance(content_file, list):
            return f"Error: '{file_path}' is a directory, not a file."
        decoded = content_file.decoded_content.decode("utf-8", errors="replace")
        return decoded
    except Exception as e:
        return f"Error reading file: {e}"


def list_dir(ctx: Context, dir_path: str = "", ref: str | None = None) -> str:
    """List directory contents from the repository at a specific git ref.

    Args:
        dir_path: Path to the directory in the repository (leave empty for root).
        ref: Branch name, tag, or commit SHA. Defaults to the repo default branch.

    Returns:
        Formatted listing of files and subdirectories.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    clean_path = dir_path.strip().strip("/")
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        if ref is not None:
            kwargs["ref"] = ref

        candidate_paths = [clean_path]
        if clean_path and not clean_path.startswith("src/"):
            candidate_paths.append(f"src/{clean_path}")

        contents = None
        last_exc: Exception | None = None
        for p in candidate_paths:
            try:
                contents = repo.get_contents(p, **kwargs)
                break
            except Exception as exc:
                last_exc = exc
                continue

        if contents is None:
            return f"Error listing directory: {last_exc}"

        if not isinstance(contents, list):
            return (
                f"Error: '{dir_path}' is a file, not a directory. "
                "Use read_file to view its contents."
            )

        lines = [f"Directory listing for '{clean_path or '/'}':"]
        sorted_entries = sorted(
            contents,
            key=lambda c: (
                0 if getattr(c, "type", "") == "dir" else 1,
                getattr(c, "name", "").lower(),
            ),
        )
        max_entries = 100
        for item in sorted_entries[:max_entries]:
            item_type = getattr(item, "type", "file")
            item_name = getattr(item, "name", "")
            item_path = getattr(item, "path", "")
            item_size = getattr(item, "size", 0)
            prefix = "[DIR] " if item_type == "dir" else f"[FILE] ({item_size} bytes) "
            lines.append(f"  {prefix}{item_name} -> {item_path}")

        if len(contents) > max_entries:
            lines.append(f"  ... and {len(contents) - max_entries} more entries")

        return "\n".join(lines)
    except Exception as e:
        return f"Error listing directory: {e}"


def create_issue(
    ctx: Context,
    title: str,
    body: str,
) -> str:
    """Create a new GitHub issue in the repository.

    Use this tool to autonomously spawn well-defined tasks, bug reports,
    or refactoring task specifications for human maintainer review. Note that
    the agent cannot label issues; labels must be attached by human maintainers.

    Args:
        title: Issue title.
        body: Markdown issue description and structured task specification.

    Returns:
        A string describing the result, including issue number and URL.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.create_issue(title=title, body=body)
        return (
            f"Successfully created issue #{issue.number} ({getattr(issue, 'html_url', 'created')})."
        )
    except Exception as e:
        return f"Error creating issue: {e}"


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


# ---------------------------------------------------------------------------
# Pulls API (PR-specific extensions)
# ---------------------------------------------------------------------------


def _collect_pr_patches(pr: Any) -> dict[str, str | None]:
    """Collect ``{filename: patch}`` for the grounding check. Never raises.

    Returns an empty mapping when no patch text is available, so the caller can tell
    "cannot verify" apart from "verified nothing" instead of silently passing.
    """
    try:
        return {
            str(file.filename): getattr(file, "patch", None)
            for file in pr.get_files()
            if getattr(file, "filename", None)
        }
    except Exception as err:
        logger.debug("Could not collect PR patches for the grounding check: %s", err)
        return {}


# Backward-compatibility alias for internal callers
_grounding_findings = extract_grounding_findings


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
    formal_eligible = state_dict.get("formal_review_eligible")
    if formal_eligible is None:
        formal_eligible = state_dict.get("user:formal_review_eligible")
    if formal_eligible is False:
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

        patches: dict[str, str | None] = {}
        retrieval_ok: bool | None = None
        # Collect changed files and patches for grounding, and log warning if Python files changed without pre-audit
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
                            patches[fn] = getattr(f, "patch", None)
                    retrieval_ok = True
                except Exception as files_err:
                    logger.debug("Could not inspect PR files for investigation gate: %s", files_err)
                    retrieval_ok = False

            modifies_python = any(f.endswith(".py") for f in changed_files)
            if modifies_python:
                deterministic_precompiled = bool(state_dict.get("deterministic_precompiled_ast"))
                if not deterministic_precompiled:
                    logger.warning(
                        "Python files modified in %s#%s, but no pre-audit ran.",
                        repo_name,
                        pr_number,
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

        # Adversarial Critic filtering: discard low-confidence (<8) and NIT issues before enforcement
        from webhook_agent.review.critic import CriticEngine

        critic_engine = CriticEngine()
        review_obj.critical_issues = critic_engine.filter_issues(review_obj.critical_issues)
        review_obj.minor_suggestions = critic_engine.filter_issues(review_obj.minor_suggestions)
        critical_list = review_obj.critical_issues
        minor_list = review_obj.minor_suggestions

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
            if kind == "critical" and not fix:
                invalid_findings.append(
                    f"critical issue on '{issue.path}:{issue.line}' lacks concrete suggested_fix replacement code"
                )
            elif fix and any(bp in fix for bp in forbidden_boilerplate):
                invalid_findings.append(
                    f"{kind} issue on '{issue.path}:{issue.line}' has generic boilerplate suggested_fix ('{issue.suggested_fix}')"
                )

        # Check REQUEST_CHANGES without actionable issues
        effective_event = (event or getattr(review_obj, "verdict", None) or "COMMENT").upper()
        if effective_event == "REQUEST_CHANGES":
            if not is_sync and not critical_list:
                invalid_findings.append(
                    "REQUEST_CHANGES event specified, but critical_issues is empty. "
                    "You must provide at least one actionable critical issue with exact path, line, and replacement code, or set event to APPROVE."
                )

        # Deterministic citation grounding: check every cited path/line against the diff.
        # Always logged; only enforced when STRICT_REVIEW_GROUNDING is set, because a
        # verifier with a false-positive bug can otherwise reject reviews into exhaustion.
        from webhook_agent.review.grounding import (
            diff_text_from_patches,
            hard_reasons,
            verify_citations,
            verify_resolutions,
        )

        grounding_patches: dict[str, str | None] = {}
        if retrieval_ok is True:
            grounding_patches = patches
        elif retrieval_ok is None:
            grounding_patches = _collect_pr_patches(pr)

        diff_text_val = diff_text_from_patches(grounding_patches) if grounding_patches else ""
        if is_sync and hasattr(review_obj, "resolutions") and review_obj.resolutions:
            grounded_res = verify_resolutions(
                review_obj.resolutions,
                diff_text_val,
                list(grounding_patches or []),
            )
            # Update review_obj.resolutions with grounded status
            from webhook_agent.review.schemas import SyncResolutionItem

            review_obj.resolutions = [SyncResolutionItem.model_validate(r) for r in grounded_res]
            resolutions_list = review_obj.resolutions
            parsed_data["resolutions"] = [r.model_dump() for r in resolutions_list]
            body = json.dumps(parsed_data)

        if effective_event == "REQUEST_CHANGES" and is_sync:
            has_unresolved = any(r.status == "UNRESOLVED" for r in resolutions_list)
            if not critical_list and not has_unresolved:
                invalid_findings.append(
                    "REQUEST_CHANGES event specified, but critical_issues is empty and no prior items are UNRESOLVED. "
                    "You must provide concrete critical_issues with path, line, and replacement code, or set event to APPROVE."
                )

        has_usable_patches = any(patch for patch in grounding_patches.values())
        if not has_usable_patches:
            logger.warning(
                "Grounding check skipped for %s#%s: no patch text available to verify "
                "citations against. Treating citations as unverified, not as valid.",
                repo_name,
                pr_number,
            )
            if effective_event == "APPROVE":
                from webhook_agent.review.review_enforcer import has_grounding_bypass_label

                if has_grounding_bypass_label(pr):
                    logger.warning(
                        "Strict Grounding Gate: Bypassed downgrade via emergency label on %s#%s (no patches)",
                        repo_name,
                        pr_number,
                    )
                else:
                    logger.warning(
                        "Grounding check skipped for %s#%s; downgrading verdict from APPROVE to COMMENT",
                        repo_name,
                        pr_number,
                    )
                    effective_event = "COMMENT"
                    event = "COMMENT"
                    if hasattr(review_obj, "verdict"):
                        review_obj.verdict = "COMMENT"
                    parsed_data["verdict"] = "COMMENT"
                    body = json.dumps(parsed_data)
        else:
            grounding_issues = verify_citations(
                _grounding_findings(review_obj),
                diff_text_val,
                changed_paths=list(grounding_patches),
            )
            if grounding_issues:
                logger.info(
                    "🔎 [Grounding] %s#%s: %d ungrounded citation(s) %s",
                    repo_name,
                    pr_number,
                    len(grounding_issues),
                    [f"{issue.severity}:{issue.code}" for issue in grounding_issues],
                )
            if strict_grounding_enabled():
                from webhook_agent.review.review_enforcer import has_grounding_bypass_label

                if has_grounding_bypass_label(pr):
                    logger.warning(
                        "Strict Grounding Gate: Bypassed via emergency label on %s#%s",
                        repo_name,
                        pr_number,
                    )
                else:
                    invalid_findings.extend(hard_reasons(grounding_issues))

        if invalid_findings:
            error_details = "; ".join(invalid_findings)
            logger.warning("Rejecting invalid review tool call: %s", error_details)
            return (
                f"Error: Review submission rejected. Invalid findings: {error_details}. "
                "Please re-examine the diff and call review() again with precise file paths, line numbers, and actionable replacement code in suggested_fix."
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


def resolve_review_thread(ctx: Context, thread_id: str) -> str:
    """Resolve an inline review comment thread on GitHub by GraphQL thread ID.

    Args:
        ctx: Agent execution context.
        thread_id: The GraphQL node ID of the review thread to resolve (e.g., 'PRRT_kwDOTKuocc5...').

    Returns:
        A clinical status string reporting whether the thread was resolved.
    """
    from webhook_agent.github.thread_resolver import (
        extract_token_from_gh,
    )
    from webhook_agent.github.thread_resolver import (
        resolve_review_thread as _resolve_graphql_thread,
    )

    try:
        gh = _get_gh_from_ctx(ctx)
        token = extract_token_from_gh(gh)
        if not token:
            return "Error: Could not extract authentication token to resolve thread."

        success = _resolve_graphql_thread(thread_id, token)
        if success:
            return f"Successfully resolved review thread {thread_id}."
        return f"Failed to resolve review thread {thread_id}. Check permissions or thread ID."
    except Exception as exc:
        return f"Error resolving review thread: {exc}"


def resolve_bot_review_threads(
    ctx: Context, pr_number: int, bot_login: str = "coderabbitai"
) -> str:
    """Find and resolve all open inline review comment threads started by a bot on a PR.

    Args:
        ctx: Agent execution context.
        pr_number: The pull request number.
        bot_login: The GitHub username/slug of the bot whose threads should be resolved (default: 'coderabbitai').

    Returns:
        A clinical summary string reporting which review threads were resolved.
    """
    from webhook_agent.github.thread_resolver import (
        auto_resolve_coderabbit_threads,
        extract_token_from_gh,
    )

    try:
        gh = _get_gh_from_ctx(ctx)
        repo_name = _get_repo_full_name(ctx)
        token = extract_token_from_gh(gh)
        if not token:
            return "Error: Could not extract authentication token to resolve bot threads."

        resolved = auto_resolve_coderabbit_threads(
            repo_name,
            pr_number,
            token,
            target_logins=(bot_login, f"{bot_login}[bot]"),
        )
        if resolved:
            return (
                f"Successfully resolved {len(resolved)} review thread(s) from {bot_login} "
                f"on {repo_name}#{pr_number}: {', '.join(resolved)}."
            )
        return f"No open review threads from {bot_login} were found to resolve on {repo_name}#{pr_number}."
    except Exception as exc:
        return f"Error resolving bot review threads: {exc}"
