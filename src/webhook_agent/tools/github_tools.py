"""Core GitHub ADK tool functions (Files, Issues, and Pulls API).

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from github import Github
from google.adk.agents.context import Context

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.review.grounding import (
    extract_grounding_findings,
    strict_grounding_enabled,
)
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.schemas import CodeReviewResponse, SyncReviewResponse
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


def _format_numbered_slice(
    path: str,
    text: str,
    start_line: int | None = None,
    max_lines: int | None = None,
) -> str:
    """Render file content with 1-based line-number prefixes, optionally windowed.

    Args:
        path: Repo-relative path, used in the header.
        text: Decoded file content.
        start_line: 1-based first line to include.
        max_lines: Maximum number of lines to include.

    Returns:
        Header line plus ``NN│ code`` rows.
    """
    all_lines = text.splitlines()
    total = len(all_lines)
    if total == 0:
        return f"{path}: (empty file)"

    first = max(1, start_line or 1)
    if first > total:
        noun = "line" if total == 1 else "lines"
        return f"{path}: start_line {first} is past end of file ({total} {noun})"

    last = total if max_lines is None else min(total, first + max(0, max_lines) - 1)
    window = all_lines[first - 1 : last]
    width = len(str(last))
    body = "\n".join(f"{first + offset:>{width}}│ {line}" for offset, line in enumerate(window))

    header = f"{path}: lines {first}-{last} of {total}"
    if last < total:
        header += f" (call again with start_line={last + 1} for more)"
    return f"{header}\n{body}"


def read_file(
    ctx: Context,
    file_path: str,
    ref: str | None = None,
    start_line: int | None = None,
    max_lines: int | None = None,
) -> str:
    """Read a file from the repository at a specific git ref, with line numbers.

    Output rows look like ``42│ some_code()``. The ``NN│ `` prefix is display only:
    copy line numbers into citations, but never copy the prefix into code or a
    suggested fix.

    Args:
        file_path: Path to the file in the repository.
        ref: Branch name, tag, or commit SHA. Defaults to the repo default branch.
        start_line: 1-based line to start from. Defaults to the first line.
        max_lines: Maximum number of lines to return. Defaults to the whole file.

    Returns:
        Header plus line-numbered content, or an error string.
    """
    clean_path = file_path.strip().strip("/")
    candidate_paths = [clean_path]
    if not clean_path.startswith("src/"):
        candidate_paths.append(f"src/{clean_path}")
    else:
        candidate_paths.append(clean_path[4:])

    # 1. Prefer reading directly from the active PR workspace sandbox at PR head
    if ref is None:
        from webhook_agent.environment.context import get_active_environment

        env = get_active_environment()
        if env is not None:
            for p in candidate_paths:
                try:
                    resolved = env.resolve_path(p)
                    if resolved is not None and resolved.is_file():
                        decoded = resolved.read_text(encoding="utf-8", errors="replace")
                        return _format_numbered_slice(p, decoded, start_line, max_lines)
                    elif resolved is not None and resolved.is_dir():
                        return f"Error: '{file_path}' is a directory, not a file."
                except Exception as env_err:
                    logger.debug("Sandbox file read failed for %s: %s", p, env_err)
                    break

    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        target_ref = ref
        if target_ref is None and hasattr(ctx, "state") and isinstance(ctx.state, dict):
            target_ref = (
                ctx.state.get("head_sha")
                or ctx.state.get("user:head_sha")
                or ctx.state.get("analyzed_sha")
                or ctx.state.get("user:analyzed_sha")
            )
        if target_ref is not None:
            kwargs["ref"] = target_ref

        content_file = None
        matched_path = clean_path
        last_exc: Exception | None = None
        for p in candidate_paths:
            try:
                content_file = repo.get_contents(p, **kwargs)
                matched_path = p
                break
            except Exception as exc:
                last_exc = exc
                continue

        if content_file is None:
            return f"Error reading file: {last_exc}"

        if isinstance(content_file, list):
            return f"Error: '{file_path}' is a directory, not a file."
        decoded = content_file.decoded_content.decode("utf-8", errors="replace")
        return _format_numbered_slice(matched_path, decoded, start_line, max_lines)
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
    clean_path = dir_path.strip().strip("/")
    candidate_paths = [clean_path]
    if clean_path and not clean_path.startswith("src/"):
        candidate_paths.append(f"src/{clean_path}")

    # 1. Prefer reading directly from active PR workspace sandbox at PR head
    if ref is None:
        from webhook_agent.environment.context import get_active_environment

        env = get_active_environment()
        if env is not None:
            for p in candidate_paths:
                try:
                    resolved = env.resolve_path(p or ".")
                    if resolved is not None and resolved.is_dir():
                        lines = [f"Directory listing for '{p or '/'}':"]
                        entries = [e for e in resolved.iterdir() if not e.name.startswith(".")]
                        sorted_path_entries = sorted(
                            entries,
                            key=lambda c: (
                                0 if c.is_dir() else 1,
                                c.name.lower(),
                            ),
                        )
                        max_entries = 100
                        for p_item in sorted_path_entries[:max_entries]:
                            item_type = "dir" if p_item.is_dir() else "file"
                            item_size = p_item.stat().st_size if p_item.is_file() else 0
                            prefix = (
                                "[DIR] " if item_type == "dir" else f"[FILE] ({item_size} bytes) "
                            )
                            rel_p = str(p_item.relative_to(env.working_dir))
                            lines.append(f"  {prefix}{p_item.name} -> {rel_p}")
                        if len(entries) > max_entries:
                            lines.append(f"  ... and {len(entries) - max_entries} more entries")
                        return "\n".join(lines)
                    elif resolved is not None and resolved.is_file():
                        return (
                            f"Error: '{dir_path}' is a file, not a directory. "
                            "Use read_file to view its contents."
                        )
                except Exception as env_err:
                    logger.debug("Sandbox list_dir failed for %s: %s", p, env_err)
                    break

    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        target_ref = ref
        if target_ref is None and hasattr(ctx, "state") and isinstance(ctx.state, dict):
            target_ref = (
                ctx.state.get("head_sha")
                or ctx.state.get("user:head_sha")
                or ctx.state.get("analyzed_sha")
                or ctx.state.get("user:analyzed_sha")
            )
        if target_ref is not None:
            kwargs["ref"] = target_ref

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
    may_create_issue = bool(
        ctx.state.get("may_create_issue") or ctx.state.get("user:may_create_issue")
    )
    if not may_create_issue:
        return (
            "Error creating issue: Creation of issues is disallowed by policy "
            "(may_create_issue is False for this event)."
        )

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
    When individual file patches are omitted by GitHub's files API (e.g. large lockfiles),
    falls back to fetching and parsing the raw PR unified diff.
    """
    try:
        patches: dict[str, str | None] = {}
        missing_patches = False
        for file in pr.get_files():
            fn = getattr(file, "filename", None)
            if fn:
                p = getattr(file, "patch", None)
                patches[str(fn)] = p
                if not p:
                    missing_patches = True

        if missing_patches or not any(patches.values()):
            from webhook_agent.github.pr_context import fetch_pr_raw_diff
            from webhook_agent.tools.diff_tools import parse_patches_from_unified_diff

            raw_diff = fetch_pr_raw_diff(pr)
            if raw_diff:
                parsed = parse_patches_from_unified_diff(raw_diff)
                for path, patch in parsed.items():
                    if (path in patches and not patches[path]) or path not in patches:
                        patches[path] = patch

        return patches
    except Exception as err:
        logger.debug("Could not collect PR patches for the grounding check: %s", err)
        return {}


# Backward-compatibility alias for internal callers
_grounding_findings = extract_grounding_findings

REQUIRED_AUDIT_DIMENSIONS: tuple[str, ...] = (
    "Logic & Boundaries",
    "Concurrency & State",
    "Security & Secrets",
    "Contract Integrity",
    "Test Coverage & Regressions",
)


def _matches_audit_dimension(dim: str, target: str) -> bool:
    d = dim.strip().lower()
    t = target.strip().lower()
    if d == t:
        return True
    if "logic" in t and ("logic" in d or "boundar" in d):
        return True
    if "concurr" in t and ("concurr" in d or "state" in d or "race" in d):
        return True
    if "contract" in t and "contract" in d:
        return True
    if ("security" in t or "secret" in t) and ("security" in d or "secret" in d):
        return True
    return bool(("test" in t or "coverage" in t) and ("test" in d or "coverage" in d))


def get_missing_scorecard_dimensions(scorecard: Sequence[Any]) -> list[str]:
    """Return any of the 5 canonical audit dimensions omitted from the scorecard."""
    missing: list[str] = []
    dims = [
        getattr(item, "dimension", None)
        or (item.get("dimension") if isinstance(item, dict) else "")
        or ""
        for item in scorecard or []
    ]
    for required in REQUIRED_AUDIT_DIMENSIONS:
        if not any(_matches_audit_dimension(d, required) for d in dims):
            missing.append(required)
    return missing


def review(
    ctx: Context,
    pr_number: int,
    body: str | CodeReviewResponse | SyncReviewResponse | None = None,
    event: str | None = None,
    *,
    report: CodeReviewResponse | SyncReviewResponse | None = None,
) -> str:
    """Submit a formal review on a pull request.

    Accepts either a typed `report` (CodeReviewResponse or SyncReviewResponse)
    or a JSON-serialized `body` string for backwards compatibility.

    Args:
        pr_number: Pull request number.
        body: Review payload (strictly a JSON string conforming to CodeReviewResponse or SyncReviewResponse).
        event: Review event type. One of APPROVE, COMMENT, REQUEST_CHANGES. Defaults to report verdict.
        report: Strongly typed CodeReviewResponse or SyncReviewResponse model instance.

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
        modifies_python: bool = False
        # Collect changed files and patches for grounding, and log warning if Python files changed without pre-audit
        skip_gate = state_dict.get("skip_investigation_gate", False)
        if not skip_gate:
            changed_files: list[str] = []
            if "deterministic_changed_files" in state_dict:
                changed_files = list(state_dict.get("deterministic_changed_files") or [])
            else:
                try:
                    patches = _collect_pr_patches(pr)
                    changed_files = list(patches.keys())
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

        # Ingestion: accept typed report directly, or extract and parse from body JSON
        from webhook_agent.review.schemas import CodeReviewResponse, SyncReviewResponse

        review_obj: SyncReviewResponse | CodeReviewResponse
        parsed_data: dict[str, Any] | None = None
        if isinstance(body, (CodeReviewResponse, SyncReviewResponse)):
            report = body
            body = None
        elif isinstance(report, str) and body is None:
            body = report
            report = None

        if report is not None:
            review_obj = report
            is_sync = isinstance(review_obj, SyncReviewResponse)
            parsed_data = review_obj.model_dump()
            critical_list = review_obj.critical_issues
            minor_list = review_obj.minor_suggestions
            resolutions_list = getattr(review_obj, "resolutions", [])
        else:
            if not body or not isinstance(body, str):
                return (
                    "Error: Review submission rejected. You must provide either a structured 'report' "
                    "or a JSON string 'body'."
                )
            from webhook_agent.review.formatter import extract_json_payload

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

        from webhook_agent.review.critic import CriticEngine

        critic_engine = CriticEngine()

        # Hunter & Critic Pipeline: if candidate_findings are present, verify through CriticEngine
        if review_obj.candidate_findings:
            from webhook_agent.review.hunter import HunterEngine

            hunter_engine = HunterEngine()
            parsed_candidates = hunter_engine.parse_candidate_findings(
                review_obj.candidate_findings
            )
            verdicts = (
                {v.finding_id: v for v in review_obj.verification_verdicts if v.finding_id}
                if review_obj.verification_verdicts
                else None
            )
            survivor_issues = critic_engine.filter_and_verify(parsed_candidates, verdicts=verdicts)
            if not review_obj.critical_issues and not review_obj.minor_suggestions:
                for issue in survivor_issues:
                    if getattr(issue, "severity", None) in ("CRITICAL", "HIGH"):
                        review_obj.critical_issues.append(issue)
                    else:
                        review_obj.minor_suggestions.append(issue)

        # Adversarial Critic filtering: discard low-confidence (<8) and NIT issues before enforcement
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

        # Strict Scorecard Gate: Enforce that code reviews evaluate all 5 audit dimensions
        if modifies_python and not skip_gate:
            missing_dims = get_missing_scorecard_dimensions(review_obj.scorecard)
            if missing_dims:
                invalid_findings.append(
                    f"scorecard is missing required audit dimension(s): {missing_dims}. "
                    "The review MUST evaluate all 5 audit dimensions ('Logic & Boundaries', "
                    "'Concurrency & State', 'Security & Secrets', 'Contract Integrity', "
                    "'Test Coverage & Regressions')."
                )

        # Validate critical issues (strict blockers requiring exact path, line, and replacement code)
        for issue in critical_list:
            p = (issue.path or "").strip().lower()
            if not p or p in ("codebase", "unknown"):
                invalid_findings.append(
                    f"critical issue '{issue.description[:50]}...' lacks exact file path (got '{issue.path}')"
                )
            if issue.line is None or issue.line <= 0:
                invalid_findings.append(
                    f"critical issue on '{issue.path}' lacks valid line number (got '{issue.line}')"
                )
            fix = (issue.suggested_fix or "").strip().lower()
            if not fix:
                invalid_findings.append(
                    f"critical issue on '{issue.path}:{issue.line}' lacks concrete suggested_fix replacement code"
                )
            elif any(bp in fix for bp in forbidden_boilerplate):
                invalid_findings.append(
                    f"critical issue on '{issue.path}:{issue.line}' has generic boilerplate suggested_fix ('{issue.suggested_fix}')"
                )

        # Validate minor suggestions (advisory: allow file-level citations and unanchored suggestions)
        for issue in minor_list:
            p = (issue.path or "").strip().lower()
            if p in ("codebase", "unknown"):
                issue.path = ""
            if issue.line is not None and issue.line <= 0:
                issue.line = None
            fix = (issue.suggested_fix or "").strip().lower()
            if fix and any(bp in fix for bp in forbidden_boilerplate):
                invalid_findings.append(
                    f"suggestion issue on '{issue.path}:{issue.line}' has generic boilerplate suggested_fix ('{issue.suggested_fix}')"
                )

        # Sync normalized minor suggestions back to parsed_data & body
        parsed_data["minor_suggestions"] = [item.model_dump() for item in minor_list]
        body = json.dumps(parsed_data)

        # Check REQUEST_CHANGES without actionable issues
        effective_event = (event or getattr(review_obj, "verdict", None) or "").upper()
        if not effective_event:
            if critical_list:
                effective_event = "REQUEST_CHANGES"
            elif hasattr(review_obj, "scorecard") and review_obj.scorecard:
                from webhook_agent.review.review_enforcer import (
                    calculate_strict_verdict,
                    calculate_sync_verdict,
                )

                if isinstance(review_obj, SyncReviewResponse):
                    effective_event = calculate_sync_verdict(review_obj)
                else:
                    effective_event = calculate_strict_verdict(review_obj)
            else:
                effective_event = "COMMENT"
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
        if retrieval_ok is True and any(patches.values()):
            grounding_patches = patches
        else:
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

        # Strict Invariant Dominance Gate: Machine-proven invariants must dominate on code PRs
        if modifies_python and not skip_gate:
            if hasattr(review_obj, "semantic_invariants") and review_obj.semantic_invariants:
                from webhook_agent.review.grounding import promote_semantic_to_verified

                promoted, remaining = promote_semantic_to_verified(
                    review_obj.semantic_invariants,
                    diff_text_val,
                    changed_paths=list(grounding_patches),
                )
                if promoted:
                    for p_inv in promoted:
                        if p_inv not in review_obj.verified_invariants:
                            review_obj.verified_invariants.append(p_inv)
                    review_obj.semantic_invariants = remaining

            if not review_obj.verified_invariants and review_obj.scorecard and diff_text_val:
                from webhook_agent.review.grounding import extract_scorecard_invariants

                recovered = extract_scorecard_invariants(
                    review_obj.scorecard,
                    diff_text_val,
                    changed_paths=list(grounding_patches),
                )
                if recovered:
                    for rec in recovered:
                        if rec not in review_obj.verified_invariants:
                            review_obj.verified_invariants.append(rec)

            num_verified = len(getattr(review_obj, "verified_invariants", []) or [])
            num_semantic = len(getattr(review_obj, "semantic_invariants", []) or [])

            if num_semantic > num_verified:
                invalid_findings.append(
                    f"semantic_invariants (count {num_semantic}) outnumbers machine-proven verified_invariants "
                    f"(count {num_verified}). Machine-proven invariants must dominate on code PRs. "
                    "Anchor your invariant claims to exact diff lines (path:line) with concrete code proof."
                )
            if num_semantic > 2:
                invalid_findings.append(
                    f"semantic_invariants count ({num_semantic}) exceeds maximum allowed cap of 2. "
                    "Focus on machine-proven invariants anchored to diff hunks."
                )

            if parsed_data is not None:
                parsed_data["verified_invariants"] = list(review_obj.verified_invariants)
                parsed_data["semantic_invariants"] = list(review_obj.semantic_invariants)
                body = json.dumps(parsed_data)

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
                logger.debug(
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
            logger.warning(
                "Rejecting invalid review tool call: %s",
                error_details,
                extra={
                    "json_fields": {
                        "event_type": "review_rejected",
                        "repo": repo_name,
                        "pr_number": pr_number,
                        "invalid_reasons": invalid_findings,
                    }
                },
            )
            return (
                f"Error: Review submission rejected. Invalid findings: {error_details}. "
                "Please re-examine the diff and call review() again with precise file paths, line numbers, and actionable replacement code in suggested_fix."
            )

        is_forced = bool(
            state_dict.get("force_review")
            or state_dict.get("user:force_review")
            or state_dict.get("is_explicit_review")
            or state_dict.get("user:is_explicit_review")
        )
        if not is_forced:
            from webhook_agent.core.commands import has_review_slash_command

            cb = str(state_dict.get("comment_body") or state_dict.get("user:comment_body") or "")
            if cb and has_review_slash_command(cb):
                is_forced = True

        active_model: str | None = None
        raw_agent = getattr(ctx, "agent", None)
        if raw_agent is not None and hasattr(raw_agent, "_current_model_name"):
            cand = getattr(raw_agent, "_current_model_name", None)
            if isinstance(cand, str) and cand.strip():
                active_model = cand.strip()
        if not active_model:
            cand = (
                state_dict.get("model_name")
                or state_dict.get("user:model_name")
                or state_dict.get("model")
                or state_dict.get("user:model")
            )
            if isinstance(cand, str) and cand.strip():
                active_model = cand.strip()

        result, _submitted = _submit_formal_review(
            pr,
            body,
            effective_event,
            target_key,
            getattr(ctx, "state", None),
            analyzed_sha=str(state_dict.get("head_sha") or state_dict.get("user:head_sha") or ""),
            force=is_forced,
            model_name=active_model,
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


def submit_initial_review(
    ctx: Context,
    pr_number: int,
    report: CodeReviewResponse,
    event: str | None = None,
) -> str:
    """Submit a formal code review for a newly opened pull request.

    Args:
        ctx: Agent execution context.
        pr_number: Pull request number.
        report: Structured review report matching CodeReviewResponse schema.
        event: Optional review event type (APPROVE, COMMENT, REQUEST_CHANGES). Defaults to report verdict.

    Returns:
        A clinical status string reporting review submission result.
    """
    return review(ctx, pr_number=pr_number, report=report, event=event)


def submit_sync_review(
    ctx: Context,
    pr_number: int,
    report: SyncReviewResponse,
    event: str | None = None,
) -> str:
    """Submit a synchronization re-review for updated pull request commits.

    Args:
        ctx: Agent execution context.
        pr_number: Pull request number.
        report: Structured review report matching SyncReviewResponse schema.
        event: Optional review event type (APPROVE, COMMENT, REQUEST_CHANGES). Defaults to report verdict.

    Returns:
        A clinical status string reporting review submission result.
    """
    return review(ctx, pr_number=pr_number, report=report, event=event)


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
