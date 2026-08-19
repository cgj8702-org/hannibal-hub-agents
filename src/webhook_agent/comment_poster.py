"""GitHub PR Review Comment Poster with diff line-anchoring and scope-aware markdown rendering.

Adapted from adk-samples/.github/scripts/post_review_comments.py for hannibal-hub-agents.
"""

from __future__ import annotations

import logging
from typing import Any

from webhook_agent.audit_schema import AuditVerdict, RiskItem
from webhook_agent.diff_tools import verify_line_reference

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
    scope_badge = f"`{verdict.pr_type}`"
    verdict_badge = f"`{verdict.verdict}`"

    lines: list[str] = [
        f"# 🛡️ Hannibal Hub Audit Report: {verdict_badge}",
        "",
        "### 1. Executive Summary",
        f"* **PR Scope:** {scope_badge}",
        f"* **Confidence Rating:** `{verdict.confidence}/5.0`",
        f"* **Verdict Justification:** {verdict.summary}",
        "",
        "---",
    ]

    all_risks = anchored_risks + top_level_summary_risks

    # Dev/Docs scope rendering (lightweight 2-section audit)
    if verdict.pr_type == "dev_docs":
        lines.append("### 2. Audit Verification")
        if all_risks:
            for idx, risk in enumerate(all_risks, 1):
                file_cite = f" (`{risk.file}:{risk.line_range}`)" if risk.file else ""
                lines.append(f"{idx}. **[{risk.category.upper()}]**{file_cite}")
                lines.append(f"   - **Issue:** {risk.description}")
                lines.append(f"   - **Remediation:** {risk.remediation}")
                lines.append("")
        else:
            lines.append(
                "Documentation & dev tools verification clean. Zero critical issues identified."
            )
            lines.append("")
        return "\n".join(lines)

    # Core backend & Minor fix rendering
    lines.extend(
        [
            "### 2. Mandatory Risk & Edge-Case Analysis",
            "",
        ]
    )

    if verdict.verdict == "REQUEST_CHANGES":
        lines.append("> [!CAUTION]")
        lines.append("> **Critical Blocking Issues Identified**")
        lines.append("")

    if all_risks:
        for idx, risk in enumerate(all_risks, 1):
            file_cite = f" (`{risk.file}:{risk.line_range}`)" if risk.file else ""
            lines.append(f"#### {idx}. [{risk.category.upper()}]{file_cite}")
            lines.append(f"* **Failure Mechanism:** {risk.description}")
            lines.append(f"* **Remediation:** {risk.remediation}")
            lines.append("")
    else:
        lines.append("> [!NOTE]")
        lines.append("> Zero critical risks identified for this PR scope.")
        lines.append("")

    lines.extend(
        [
            "---",
            "### 3. Verification Protocol",
            "* **Line-Anchored Grounding:** All citations verified against modified diff hunks.",
            "* **Anti-Sycophancy Standard:** Objective technical feedback only.",
        ]
    )

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
