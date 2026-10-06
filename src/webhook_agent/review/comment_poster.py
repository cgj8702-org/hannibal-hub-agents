"""GitHub PR Review Comment Poster with diff line-anchoring and scope-aware markdown rendering.

Adapted from adk-samples/.github/scripts/post_review_comments.py for hannibal-hub-agents.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from github import Github

from ..schemas import IssueItem
from ..tools.diff_tools import (
    _norm,
    _strip_diff_prefix,
    check_window,
    walk_right_side,
)
from .duplicate_detector import already_raised, build_exclusions, group_repeated_findings

logger = logging.getLogger("webhook_agent.review.comment_poster")


def is_echo_suggestion(
    suggested_fix: str,
    file_lines: dict[int, str],
    line: int,
    start_line: int | None = None,
) -> bool:
    """Check if suggested_fix is an echo (verbatim copy) of existing diff lines at or starting from line."""
    if not suggested_fix or not file_lines:
        return False

    code = suggested_fix.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", code)
        code = re.sub(r"\n?```$", "", code)
    code = "\n".join(ln.strip() for ln in code.splitlines() if ln.strip())
    if not code:
        return False

    norm_fix = _norm(code)
    if not norm_fix:
        return False

    # 1. If start_line is provided and distinct from line: check explicit range
    if start_line is not None and start_line != line:
        s, e = min(start_line, line), max(start_line, line)
        range_lines = [file_lines[ln] for ln in range(s, e + 1) if ln in file_lines]
        if range_lines and norm_fix == _norm("".join(range_lines)):
            return True

    # 2. Single-line comparison at line
    if line in file_lines and norm_fix == _norm(file_lines[line]):
        return True

    # 3. Multi-line slice comparison:
    # If the suggestion has N lines, check if it matches a forward slice [line, line + N - 1]
    # or backward slice [line - N + 1, line]
    lines_list = code.splitlines()
    n_lines = len(lines_list)
    if n_lines > 1:
        fwd = [file_lines[ln] for ln in range(line, line + n_lines) if ln in file_lines]
        if len(fwd) == n_lines and norm_fix == _norm("".join(fwd)):
            return True

        bwd = [file_lines[ln] for ln in range(line - n_lines + 1, line + 1) if ln in file_lines]
        if len(bwd) == n_lines and norm_fix == _norm("".join(bwd)):
            return True

        # Forward non-empty line scan starting at line
        fwd_non_empty = [
            file_lines[ln]
            for ln in sorted(file_lines.keys())
            if ln >= line and file_lines[ln].strip()
        ][:n_lines]
        if len(fwd_non_empty) == n_lines and norm_fix == _norm("".join(fwd_non_empty)):
            return True

    return False


def is_echo_description(description: str) -> bool:
    """Check if the description is merely an observational remark accompanying an echo suggestion."""
    desc_lower = (description or "").strip().lower()
    if not desc_lower:
        return True
    echo_prefixes = (
        "ensure",
        "verify",
        "check that",
        "make sure",
        "confirm that",
        "note:",
        "classification checks",
    )
    return any(desc_lower.startswith(p) for p in echo_prefixes)


def filter_echo_suggestions(issues: list[Any], diff_text: str) -> list[Any]:
    """Filter out issues whose suggested_fix is an exact echo of the diff lines."""
    if not issues or not diff_text:
        return issues

    _, text_by_line = walk_right_side(diff_text)
    filtered = []
    for issue in issues:
        p_val = getattr(issue, "path", None) or (
            issue.get("path") if isinstance(issue, dict) else None
        )
        l_val = getattr(issue, "line", None) or (
            issue.get("line") if isinstance(issue, dict) else None
        )
        s_val = getattr(issue, "start_line", None) or (
            issue.get("start_line") if isinstance(issue, dict) else None
        )
        fix = getattr(issue, "suggested_fix", None) or (
            issue.get("suggested_fix") if isinstance(issue, dict) else None
        )
        if not p_val or l_val is None or not fix:
            filtered.append(issue)
            continue

        clean_path = _strip_diff_prefix(str(p_val))
        fl = text_by_line.get(clean_path) or text_by_line.get(str(p_val)) or {}
        if isinstance(l_val, int) and is_echo_suggestion(
            fix, fl, l_val, start_line=s_val if isinstance(s_val, int) else None
        ):
            logger.info("Filtered echo suggestion finding at %s:%s", clean_path, l_val)
            continue
        filtered.append(issue)
    return filtered


def format_suggestion_body(
    description: str,
    suggested_fix: str | None = None,
    is_multiline_anchor: bool = True,
) -> str:
    """Format an inline review comment body with code formatting.

    If suggested_fix has multiple lines and is anchored to a single line without a range,
    renders a ```python code block to prevent destructive one-line diff bombs in GitHub's UI.
    Uses ```suggestion for single-line changes or multi-line changes with verified range anchors.
    """
    desc = (description or "").strip()
    if not suggested_fix or not suggested_fix.strip():
        return desc

    code = suggested_fix.strip("\r\n")
    if code.strip().startswith("```"):
        code = code.strip()
        code = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", code)
        code = re.sub(r"\n?```$", "", code)

    code = code.rstrip()
    if not code:
        return desc

    has_multiple_lines = "\n" in code.strip()
    if has_multiple_lines and not is_multiline_anchor:
        logger.info(
            "Multi-line suggestion on single-line anchor: formatting as ```python block to prevent diff bomb"
        )
        return f"{desc}\n\n```python\n{code}\n```"

    return f"{desc}\n\n```suggestion\n{code}\n```"


def build_github_review_comments(
    issues: list[IssueItem],
    diff_text: str,
    existing_comments: list[dict[str, Any]] | None = None,
    max_comments: int | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Convert diff-anchored IssueItems into GitHub review comment payloads.

    Uses walk_right_side and check_window to snap off-by-a-few line references
    back onto valid added diff lines. Also suppresses duplicates from past comments
    and groups 3+ identical defect classes into single summary comments.

    Returns:
        (inline_comments, anchored_keys):
        - inline_comments: list of dicts suitable for GitHub create_review(comments=[...]):
          [{"path": path, "line": line, "side": "RIGHT", "body": body}]
        - anchored_keys: set of "{path}:{line}" for issues that were successfully anchored inline.
    """
    inline_comments: list[dict[str, Any]] = []
    anchored_keys: set[str] = set()

    anchors, text_by_line = walk_right_side(diff_text)
    zones, texts = build_exclusions(existing_comments or []) if existing_comments else ({}, [])

    # Group 3+ repeated defect classes into single comments
    processed_issues = group_repeated_findings(issues)

    for issue in processed_issues:
        if max_comments is not None and len(inline_comments) >= max_comments:
            logger.info(
                "Reached comment budget (%d). Truncating further inline comments.", max_comments
            )
            break

        path_val = getattr(issue, "path", None) or (
            issue.get("path") if isinstance(issue, dict) else None
        )
        line_val = getattr(issue, "line", None) or (
            issue.get("line") if isinstance(issue, dict) else None
        )
        start_line_val = getattr(issue, "start_line", None) or (
            issue.get("start_line") if isinstance(issue, dict) else None
        )
        desc_val = getattr(issue, "description", "") or (
            issue.get("description", "") if isinstance(issue, dict) else ""
        )
        fix_val = getattr(issue, "suggested_fix", "") or (
            issue.get("suggested_fix", "") if isinstance(issue, dict) else ""
        )
        win_val = getattr(issue, "window", "") or (
            issue.get("window", "") if isinstance(issue, dict) else ""
        )

        if not path_val or line_val is None:
            continue

        clean_path = _strip_diff_prefix(str(path_val))
        file_anchors = anchors.get(clean_path) or anchors.get(str(path_val)) or set()
        file_lines = text_by_line.get(clean_path) or text_by_line.get(str(path_val)) or {}

        target_line = int(line_val)
        final_line: int | None = None

        # Direct match on modified/added diff line
        if target_line in file_anchors:
            final_line = target_line
        else:
            # Check window and attempt line-snapping within modified hunks
            code_context = win_val or fix_val or desc_val
            if file_lines and code_context:
                verified, snapped_line, reason = check_window(code_context, target_line, file_lines)
                if verified and snapped_line in file_anchors:
                    logger.info(
                        "Snapped issue anchor at '%s:%s' -> '%s:%s' (%s)",
                        clean_path,
                        target_line,
                        clean_path,
                        snapped_line,
                        reason,
                    )
                    final_line = snapped_line

        if final_line is not None:
            s_val = int(start_line_val) if isinstance(start_line_val, int) else None

            # Echo check: if suggested_fix verbatim duplicates existing diff lines, suppress
            if fix_val and is_echo_suggestion(fix_val, file_lines, final_line, start_line=s_val):
                logger.info(
                    "Suppressed echo suggestion comment on %s:%s (suggested_fix matches diff code)",
                    clean_path,
                    final_line,
                )
                if is_echo_description(desc_val):
                    continue
                fix_val = ""

            # Range anchor verification for multi-line suggestions
            is_multiline_anchor = bool(
                s_val is not None and s_val < final_line and s_val in file_anchors
            )

            body = format_suggestion_body(
                desc_val, fix_val, is_multiline_anchor=is_multiline_anchor
            )

            # Check duplicate suppression against existing comments
            if existing_comments:
                dup_reason = already_raised(clean_path, final_line, body, zones, texts)
                if dup_reason:
                    logger.info(
                        "Suppressed duplicate comment on %s:%s (%s)",
                        clean_path,
                        final_line,
                        dup_reason,
                    )
                    continue

            comment_dict: dict[str, Any] = {
                "path": clean_path,
                "line": final_line,
                "side": "RIGHT",
                "body": body,
            }
            if is_multiline_anchor and s_val is not None:
                comment_dict["start_line"] = s_val
                comment_dict["start_side"] = "RIGHT"

            inline_comments.append(comment_dict)
            anchored_keys.add(f"{clean_path}:{final_line}")
        else:
            logger.debug(
                "Issue at '%s:%s' falls outside modified diff hunks and could not be snapped. Skipping inline comment.",
                clean_path,
                line_val,
            )

    return inline_comments, anchored_keys


def _add_eyes_reaction(gh: Github, repo_name: str, payload: dict[str, Any]) -> None:
    """Programmatically react with eyes emoji to incoming comment events."""
    try:
        canonical = payload.get("canonical", "")
        raw = payload.get("raw_payload", {})
        action = payload.get("action") or raw.get("action")
        if action == "deleted" or canonical.endswith(".deleted"):
            return

        if canonical.startswith("issue_comment."):
            issue_data = raw.get("issue", {})
            pr_data = raw.get("pull_request", {})
            issue_num = issue_data.get("number") or pr_data.get("number")
            comment_data = raw.get("comment", {})
            comment_id = comment_data.get("id")
            if issue_num and comment_id:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(int(issue_num))
                comment = issue.get_comment(int(comment_id))
                comment.create_reaction("eyes")
        elif canonical.startswith("pull_request_review_comment."):
            pr_data = raw.get("pull_request", {})
            comment_data = raw.get("comment", {})
            pr_num = pr_data.get("number")
            comment_id = comment_data.get("id")
            if pr_num and comment_id:
                repo = gh.get_repo(repo_name)
                pr = repo.get_pull(int(pr_num))
                pr_comment = pr.get_review_comment(int(comment_id))
                pr_comment.create_reaction("eyes")
        elif canonical in (
            "pull_request.opened",
            "pull_request.reopened",
            "issues.opened",
            "issues.reopened",
        ) or (
            canonical.startswith(("pull_request.", "issues.")) and action in ("opened", "reopened")
        ):
            target_num = (raw.get("pull_request") or {}).get("number") or (
                raw.get("issue") or {}
            ).get("number")
            if target_num:
                repo = gh.get_repo(repo_name)
                issue = repo.get_issue(int(target_num))
                issue.create_reaction("eyes")
    except Exception as exc:
        logger.warning("Failed to add eyes reaction to comment: %s", exc)


add_eyes_reaction = _add_eyes_reaction
