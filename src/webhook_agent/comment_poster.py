"""GitHub PR Review Comment Poster with diff line-anchoring and scope-aware markdown rendering.

Adapted from adk-samples/.github/scripts/post_review_comments.py for hannibal-hub-agents.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from webhook_agent.audit_schema import AuditVerdict, RiskItem
from webhook_agent.diff_tools import _strip_diff_prefix, verify_line_reference
from webhook_agent.schemas import IssueItem

logger = logging.getLogger("webhook_agent.comment_poster")


def sanitize_and_anchor_risks(
    risks: list[RiskItem], diff_text: str
) -> tuple[list[RiskItem], list[RiskItem]]:
    """Validate line citations against diff text using added_line_anchors logic.

    Returns:
        (anchored_risks, top_level_summary_risks): Anchored risks are safe for inline line comments;
        top_level_summary_risks are included in the top-level body summary.
    """
    anchored_risks: list[RiskItem] = []
    top_level_summary_risks: list[RiskItem] = []

    for risk in risks:
        if not risk.file or not risk.line_range:
            top_level_summary_risks.append(risk)
            continue

        line_num: int | None = None
        try:
            raw_line = risk.line_range.lstrip("L").split("-")[0]
            line_num = int(raw_line)
        except (ValueError, AttributeError):
            line_num = None

        if line_num is not None and verify_line_reference(
            diff_text, risk.file, line_num
        ):
            anchored_risks.append(risk)
        else:
            logger.warning(
                "Line reference '%s' in file '%s' is outside modified diff hunks. Moving to top-level summary.",
                risk.line_range,
                risk.file,
            )
            top_level_summary_risks.append(risk)

    return anchored_risks, top_level_summary_risks


def render_review_markdown(
    verdict: AuditVerdict,
    anchored_risks: list[RiskItem],
    top_level_summary_risks: list[RiskItem],
) -> str:
    """Render high-trust, scope-aware Markdown summary body for GitHub PR review."""
    verdict_badge = f"`{verdict.verdict}`"

    lines: list[str] = [
        f"## 🛡️ Code Review: {verdict_badge}",
        "",
        "### 1. Executive Summary",
        "",
        f"* **Summary & Justification:** {verdict.summary}",
        "",
        "---",
        "",
        "### 2. Potential Risks & Edge Cases",
        "",
    ]

    all_risks = anchored_risks + top_level_summary_risks

    if all_risks:
        for idx, risk in enumerate(all_risks, 1):
            file_cite = f" (`{risk.file}:{risk.line_range}`)" if risk.file else ""
            lines.append(f"{idx}. **[{risk.category.upper()}]**{file_cite}")
            lines.append(f"   - **Issue:** {risk.description}")
            lines.append(f"   - **Remediation:** {risk.remediation}")
            lines.append("")
    else:
        lines.append("* *None identified for this PR scope.*")
        lines.append("")

    return "\n".join(lines)


def prepare_review_payload(verdict: AuditVerdict, diff_text: str) -> dict[str, Any]:
    """Prepare validated, anchored GitHub review payload ready for submission."""
    anchored_risks, top_level_risks = sanitize_and_anchor_risks(
        verdict.risks, diff_text
    )
    body_md = render_review_markdown(verdict, anchored_risks, top_level_risks)

    payload: dict[str, Any] = {
        "event": verdict.verdict
        if verdict.verdict in ("APPROVE", "REQUEST_CHANGES", "COMMENT")
        else "COMMENT",
        "body": body_md,
        "anchored_risks_count": len(anchored_risks),
        "unanchored_risks_count": len(top_level_risks),
    }
    return payload


def format_suggestion_body(description: str, suggested_fix: str | None = None) -> str:
    """Format an inline review comment body with a clean GitHub ```suggestion block if a fix is provided."""
    desc = (description or "").strip()
    if not suggested_fix or not suggested_fix.strip():
        return desc

    code = suggested_fix.strip("\r\n")
    if code.strip().startswith("```"):
        code = code.strip()
        code = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", code)
        code = re.sub(r"\n?```$", "", code)

    code = code.rstrip()
    return f"{desc}\n\n```suggestion\n{code}\n```"


def build_github_review_comments(
    issues: list[IssueItem],
    diff_text: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Convert diff-anchored IssueItems into GitHub review comment payloads.

    Returns:
        (inline_comments, anchored_keys):
        - inline_comments: list of dicts suitable for GitHub create_review(comments=[...]):
          [{"path": path, "line": line, "side": "RIGHT", "body": body}]
        - anchored_keys: set of "{path}:{line}" for issues that were successfully anchored inline.
    """
    inline_comments: list[dict[str, Any]] = []
    anchored_keys: set[str] = set()

    for issue in issues:
        if not issue.path or issue.line is None:
            continue

        clean_path = _strip_diff_prefix(issue.path)
        if verify_line_reference(diff_text, clean_path, issue.line):
            body = format_suggestion_body(issue.description, issue.suggested_fix)
            inline_comments.append(
                {
                    "path": clean_path,
                    "line": issue.line,
                    "side": "RIGHT",
                    "body": body,
                }
            )
            anchored_keys.add(f"{clean_path}:{issue.line}")
        else:
            logger.debug(
                "Issue at '%s:%s' falls outside modified diff hunks. Skipping inline comment.",
                clean_path,
                issue.line,
            )

    return inline_comments, anchored_keys
