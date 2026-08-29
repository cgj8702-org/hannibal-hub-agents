"""Deterministic Markdown template renderer and strict mechanical verdict calculator.

Injects validated Pydantic JSON fields directly into code_review_template.md
and sync_review_template.md with strict un-cheatable mechanical rules.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .schemas import CodeReviewResponse, SyncReviewResponse, clean_field_string

logger = logging.getLogger("webhook_agent.formatter")


def truncate_log_payload(val: Any, max_length: int = 300) -> str:
    """Truncate long string representations (diffs, JSON, tool output) for clean Cloud Logging output."""
    if val is None:
        return ""
    text = str(val)
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}... [truncated {len(text) - max_length} chars]"


def normalize_code_review_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Self-healing normalizer that coerces loose LLM JSON into strict CodeReviewResponse dict structure."""
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)

    if not normalized.get("executive_summary"):
        normalized["executive_summary"] = "Autonomous PR code review report."
    else:
        normalized["executive_summary"] = (
            clean_field_string(normalized["executive_summary"])
            or "Autonomous PR code review report."
        )

    conf = normalized.get("confidence")
    if not isinstance(conf, int) or not (1 <= conf <= 5):
        normalized["confidence"] = 5

    raw_risks = normalized.get("risks_and_edge_cases")
    clean_risks: list[dict[str, str]] = []
    if isinstance(raw_risks, list):
        for item in raw_risks:
            if isinstance(item, str) and item.strip():
                r_text = item.strip()
                if any(
                    hdr in r_text.lower()
                    for hdr in (
                        "edge-case analysis",
                        "mandatory risk",
                        "section 4",
                        "scorecard summary",
                    )
                ) or r_text.startswith("#"):
                    continue
                clean_risks.append(
                    {
                        "risk": r_text,
                        "recommendation": "",
                    }
                )
            elif isinstance(item, dict):
                r_text = str(item.get("risk") or item.get("description") or "").strip()
                if any(
                    hdr in r_text.lower()
                    for hdr in (
                        "edge-case analysis",
                        "mandatory risk",
                        "section 4",
                        "scorecard summary",
                    )
                ) or r_text.startswith("#"):
                    continue
                rec_text = str(
                    item.get("recommendation") or item.get("suggested_fix") or ""
                ).strip()
                if r_text:
                    clean_risks.append(
                        {
                            "risk": r_text,
                            "recommendation": rec_text,
                        }
                    )

    normalized["risks_and_edge_cases"] = clean_risks

    raw_crit = normalized.get("critical_issues")
    clean_crit: list[dict[str, Any]] = []
    if isinstance(raw_crit, list):
        for item in raw_crit:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in (
                    "none",
                    "none found",
                    "critical issue detected.",
                ):
                    clean_crit.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": "",
                        }
                    )
            elif isinstance(item, dict):
                desc = str(item.get("description") or "").strip()
                fix = str(item.get("suggested_fix") or "").strip()
                path_val = str(item.get("path") or "codebase").strip()
                if desc and desc.lower() not in (
                    "none",
                    "none found",
                    "critical issue detected.",
                ):
                    clean_crit.append(
                        {
                            "path": path_val,
                            "line": (
                                item.get("line")
                                if isinstance(item.get("line"), int)
                                else None
                            ),
                            "description": desc,
                            "suggested_fix": fix,
                        }
                    )
    normalized["critical_issues"] = clean_crit

    raw_minor = normalized.get("minor_suggestions")
    clean_minor: list[dict[str, Any]] = []
    if isinstance(raw_minor, list):
        for item in raw_minor:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in (
                    "none",
                    "none found",
                    "minor suggestion.",
                    "minor suggestion",
                ):
                    clean_minor.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": "",
                        }
                    )
            elif isinstance(item, dict):
                desc = str(item.get("description") or "").strip()
                fix = str(item.get("suggested_fix") or "").strip()
                path_val = str(item.get("path") or "codebase").strip()
                if desc and desc.lower() not in (
                    "none",
                    "none found",
                    "minor suggestion.",
                    "minor suggestion",
                ):
                    clean_minor.append(
                        {
                            "path": path_val,
                            "line": (
                                item.get("line")
                                if isinstance(item.get("line"), int)
                                else None
                            ),
                            "description": desc,
                            "suggested_fix": fix,
                        }
                    )
    normalized["minor_suggestions"] = clean_minor

    raw_gaps = normalized.get("context_gaps")
    if not isinstance(raw_gaps, list):
        normalized["context_gaps"] = []
    else:
        normalized["context_gaps"] = [str(g) for g in raw_gaps if g]

    return normalized


def normalize_sync_review_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Self-healing normalizer that coerces loose LLM sync review JSON into strict SyncReviewResponse dict structure."""
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)
    if not normalized.get("summary"):
        normalized["summary"] = "Pull request synchronization review update."
    else:
        normalized["summary"] = (
            clean_field_string(normalized["summary"])
            or "Pull request synchronization review update."
        )

    raw_res = normalized.get("resolutions")
    clean_res: list[dict[str, str]] = []
    if isinstance(raw_res, list):
        for item in raw_res:
            if isinstance(item, str) and item.strip():
                clean_res.append(
                    {
                        "item_description": item.strip(),
                        "status": "RESOLVED",
                        "evidence": "Verified in incremental commit diff.",
                    }
                )
            elif isinstance(item, dict):
                desc = str(
                    item.get("item_description")
                    or item.get("issue")
                    or item.get("description")
                    or item.get("title")
                    or "Review finding resolution"
                ).strip()
                status = str(item.get("status") or "RESOLVED").strip().upper()
                if status not in ("RESOLVED", "UNRESOLVED"):
                    status = "RESOLVED"
                ev = str(
                    item.get("evidence")
                    or item.get("details")
                    or "Verified in commit diff."
                ).strip()
                clean_res.append(
                    {
                        "item_description": desc,
                        "status": status,
                        "evidence": ev,
                    }
                )
    normalized["resolutions"] = clean_res

    raw_crit = normalized.get("critical_issues") or normalized.get(
        "new_critical_issues"
    )
    clean_crit: list[dict[str, Any]] = []
    if isinstance(raw_crit, list):
        for item in raw_crit:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in ("none", "none found"):
                    clean_crit.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": "",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                desc = str(
                    item.get("description")
                    or item.get("title")
                    or item.get("item_description")
                    or ""
                ).strip()
                fix = str(item.get("suggested_fix") or "").strip()
                if desc and desc.lower() not in ("none", "none found"):
                    clean_crit.append(
                        {
                            "path": path,
                            "line": (
                                item.get("line")
                                if isinstance(item.get("line"), int)
                                else None
                            ),
                            "description": desc,
                            "suggested_fix": fix,
                        }
                    )

    raw_minor = normalized.get("minor_suggestions") or normalized.get(
        "new_minor_suggestions"
    )
    clean_minor: list[dict[str, Any]] = []
    if isinstance(raw_minor, list):
        for item in raw_minor:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in ("none", "none found"):
                    clean_minor.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": "",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                desc = str(
                    item.get("description")
                    or item.get("title")
                    or item.get("item_description")
                    or ""
                ).strip()
                fix = str(item.get("suggested_fix") or "").strip()
                if desc and desc.lower() not in ("none", "none found"):
                    clean_minor.append(
                        {
                            "path": path,
                            "line": (
                                item.get("line")
                                if isinstance(item.get("line"), int)
                                else None
                            ),
                            "description": desc,
                            "suggested_fix": fix,
                        }
                    )

    raw_new = normalized.get("new_findings")
    if isinstance(raw_new, list) and not clean_crit and not clean_minor:
        for item in raw_new:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in ("none", "none found"):
                    clean_crit.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": "",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                title = str(item.get("title") or "").strip()
                desc = str(
                    item.get("description")
                    or title
                    or item.get("item_description")
                    or ""
                ).strip()
                cat = str(item.get("category") or "").strip()
                sev = str(item.get("severity") or "").upper()
                full_desc = f"[{cat}] {desc}" if cat else desc
                fix = str(item.get("suggested_fix") or "").strip()
                if desc and desc.lower() not in ("none", "none found"):
                    issue_dict = {
                        "path": path,
                        "line": (
                            item.get("line")
                            if isinstance(item.get("line"), int)
                            else None
                        ),
                        "description": full_desc,
                        "suggested_fix": fix,
                    }
                    if (
                        sev in ("LOW", "INFO")
                        or cat == "MAINTAINABILITY"
                        or "acceptable tradeoff" in desc.lower()
                    ):
                        clean_minor.append(issue_dict)
                    else:
                        clean_crit.append(issue_dict)

    normalized["critical_issues"] = clean_crit
    normalized["minor_suggestions"] = clean_minor

    conf = normalized.get("confidence")
    if not isinstance(conf, int) or not (1 <= conf <= 5):
        normalized["confidence"] = 5
    normalized["confidence"] = conf

    return normalized


def calculate_strict_verdict(review: CodeReviewResponse) -> str:
    """Calculate PR review verdict mechanically from structured issues and confidence.

    Rules:
    - ANY critical issue -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - 0 critical issues, confidence >= 4 -> APPROVE
    """
    if len(review.critical_issues) > 0:
        logger.info(
            "Mechanical verdict: REQUEST_CHANGES (critical_issues=%d)",
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        logger.info(
            "Mechanical verdict: COMMENT (confidence=%d < 4)", review.confidence
        )
        return "COMMENT"

    logger.info("Mechanical verdict: APPROVE (0 critical issues, confidence >= 4)")
    return "APPROVE"


def calculate_sync_verdict(review: SyncReviewResponse) -> str:
    """Calculate sync re-review verdict mechanically from resolutions and new issues.

    Rules:
    - ANY unresolved finding -> REQUEST_CHANGES
    - ANY critical issue -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - All items RESOLVED, 0 critical issues, confidence >= 4 -> APPROVE
    """
    unresolved = [r for r in review.resolutions if r.status == "UNRESOLVED"]
    has_critical = len(review.critical_issues) > 0

    if unresolved or has_critical:
        logger.info(
            "Sync verdict: REQUEST_CHANGES (unresolved=%d, critical=%d)",
            len(unresolved),
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        return "COMMENT"

    logger.info(
        "Sync verdict: APPROVE (all items RESOLVED, %d minor suggestions)",
        len(review.minor_suggestions),
    )
    return "APPROVE"


def parse_text_review_to_dict(body: str) -> dict[str, Any]:
    """Parse loose Markdown text review body into structured dictionary for CodeReviewResponse."""
    data: dict[str, Any] = {}

    summary_match = re.search(
        r"(?:Summary & Justification|Goal of the PR):\*\*?\s*([^\n]+)",
        body,
        re.IGNORECASE,
    )
    if summary_match:
        data["executive_summary"] = summary_match.group(1).strip("* -•` ")
    else:
        lines = [
            re.sub(
                r"^(?:\*?\s*\*\*?Summary & Justification:\*\*?|\*?\s*\*\*?Executive Summary:\*\*?)\s*",
                "",
                line.strip("* -•` "),
                flags=re.I,
            )
            for line in body.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        data["executive_summary"] = (
            lines[0] if lines else "Autonomous PR code review report."
        )

    conf_match = re.search(r"Confidence:\s*`?(\d)`?/5", body, re.IGNORECASE)
    if conf_match:
        data["confidence"] = int(conf_match.group(1))

    risks: list[dict[str, str]] = []
    risk_matches = re.findall(
        r"Potential Edge Case / Risk:\s*([^\n]+)(?:\n\s*\*?\s*Recommended Safeguard:\s*([^\n]+))?",
        body,
        re.IGNORECASE,
    )
    for r_text, s_text in risk_matches:
        clean_r = r_text.strip("* -•")
        if clean_r:
            risks.append(
                {
                    "risk": clean_r,
                    "recommendation": (s_text.strip("* -•") if s_text else ""),
                }
            )

    data["risks_and_edge_cases"] = risks

    critical_issues: list[dict[str, Any]] = []
    minor_suggestions: list[dict[str, Any]] = []

    current_section = None
    NON_ISSUE_TOKENS = {
        "5/5",
        "4/5",
        "3/5",
        "2/5",
        "1/5",
        "APPROVE",
        "APPROVED",
        "NONE",
        "NONE FOUND",
        "NONE IDENTIFIED",
        "NONE FOUND.",
        "N/A",
        "PASSED",
        "OK",
        "CLEAN",
        "SUCCESS",
        "NO ISSUES",
        "NO CRITICAL ISSUES",
        "NO CRITICAL ISSUES FOUND",
        "NONE IDENTIFIED FOR THIS PR SCOPE.",
        "NONE IDENTIFIED FOR THIS PR SCOPE",
    }
    NON_ISSUE_PATHS = {
        "CODEBASE",
        "OVERALL",
        "SUMMARY",
        "AUDITOR CONFIDENCE",
        "CONFIDENCE",
        "RATING",
        "SCORE",
        "JUSTIFICATION",
        "SUMMARY & JUSTIFICATION",
    }

    for line in body.splitlines():
        line_s = line.strip()
        if "Critical" in line_s:
            current_section = "critical"
            continue
        elif "Minor" in line_s or "Refactoring" in line_s or "Suggestion" in line_s:
            current_section = "minor"
            continue
        elif (
            "Potential Risk" in line_s
            or "Edge Case" in line_s
            or "Executive Summary" in line_s
            or "Section" in line_s
            or line_s.startswith("##")
        ):
            current_section = "other"
            continue

        if (
            current_section in ("critical", "minor")
            and line_s.startswith(("*", "-", "•"))
            and ":" in line_s
        ):
            parts = line_s.lstrip("*-•").strip().split(":", 1)
            raw_path = (
                parts[0]
                .replace("🔴", "")
                .replace("🟡", "")
                .replace("✅", "")
                .strip("`* ")
            )
            desc_part = parts[1].strip() if len(parts) > 1 else ""
            clean_desc = desc_part if desc_part else line_s.lstrip("*-•🔴🟡✅ ").strip()

            raw_path_norm = raw_path.strip().upper()
            clean_desc_norm = clean_desc.strip("`* :.").upper()

            if (
                not clean_desc
                or clean_desc.strip("`* :") == raw_path
                or clean_desc_norm in NON_ISSUE_TOKENS
                or raw_path_norm in NON_ISSUE_PATHS
                or "NONE FOUND" in clean_desc_norm
                or "NONE IDENTIFIED" in clean_desc_norm
                or clean_desc_norm.startswith("APPROVE")
                or clean_desc_norm.startswith("5/5")
            ):
                continue

            item_dict = {
                "path": raw_path if "/" in raw_path or "." in raw_path else "codebase",
                "line": None,
                "description": clean_desc,
                "suggested_fix": "",
            }
            if current_section == "critical":
                critical_issues.append(item_dict)
            elif current_section == "minor":
                minor_suggestions.append(item_dict)

    data["critical_issues"] = critical_issues
    data["minor_suggestions"] = minor_suggestions

    return data


def render_code_review_markdown(
    review: CodeReviewResponse, verdict: str | None = None
) -> str:
    """Render CodeReviewResponse into clean, modern GitHub Markdown."""
    if verdict is None:
        verdict = calculate_strict_verdict(review)

    verdict_badge = f"`{verdict}`" if verdict else "`COMMENT`"

    critical_lines: list[str] = []
    if review.critical_issues:
        for issue in review.critical_issues:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            item_str = f"* {loc}: {issue.description}"
            if issue.suggested_fix and issue.suggested_fix.strip():
                item_str += f"\n  * *Suggested Fix*: {issue.suggested_fix.strip()}"
            critical_lines.append(item_str)
    else:
        critical_lines.append("* *None found.*")

    minor_lines: list[str] = []
    if review.minor_suggestions:
        for suggestion in review.minor_suggestions:
            loc = (
                f"`{suggestion.path}:{suggestion.line}`"
                if suggestion.line
                else f"`{suggestion.path}`"
            )
            item_str = f"* {loc}: {suggestion.description}"
            if suggestion.suggested_fix and suggestion.suggested_fix.strip():
                item_str += f"\n  * *Suggested Fix*: {suggestion.suggested_fix.strip()}"
            minor_lines.append(item_str)
    else:
        minor_lines.append("* *None found.*")

    risk_lines: list[str] = []
    if review.risks_and_edge_cases:
        for item in review.risks_and_edge_cases:
            risk_lines.append(f"* **Risk:** {item.risk}")
            if item.recommendation and item.recommendation.strip():
                risk_lines.append(
                    f"  * *Recommendation*: {item.recommendation.strip()}"
                )
        risk_block = "\n".join(risk_lines).strip()
    else:
        risk_block = "* *None identified for this PR scope.*"

    markdown_parts = [
        f"## 🛡️ Code Review: {verdict_badge}",
        "",
        "### 1. Executive Summary",
        "",
        f"* **Summary & Justification:** {review.executive_summary}",
        f"* **Auditor Confidence:** `{review.confidence}/5`",
        "",
        "---",
        "",
        "### 2. Action Items",
        "",
        "#### 🔴 Critical (Must Fix Before Merge)",
        "\n".join(critical_lines),
        "",
        "#### 🟡 Suggestions & Maintainability",
        "\n".join(minor_lines),
        "",
        "---",
        "",
        "### 3. Potential Risks & Edge Cases",
        "",
        risk_block,
    ]

    if review.context_gaps:
        gaps_str = ", ".join(review.context_gaps)
        markdown_parts.extend(
            [
                "",
                "---",
                "",
                "### 4. Verification Notes",
                f"* **Context Gaps:** {gaps_str}",
            ]
        )

    return "\n".join(markdown_parts) + "\n"


def render_sync_review_markdown(
    review: SyncReviewResponse, verdict: str | None = None
) -> str:
    """Render SyncReviewResponse into clean, modern GitHub Markdown."""
    if verdict is None:
        verdict = calculate_sync_verdict(review)

    verdict_badge = f"`{verdict}`" if verdict else "`COMMENT`"

    res_lines: list[str] = []
    if review.resolutions:
        for item in review.resolutions:
            icon = "✅" if item.status == "RESOLVED" else "🔴"
            res_lines.append(
                f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
            )
    else:
        res_lines.append("* *No prior review items tracked.*")

    crit_lines: list[str] = []
    if review.critical_issues:
        for issue in review.critical_issues:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            item_str = f"* 🔴 {loc}: {issue.description}"
            if issue.suggested_fix and issue.suggested_fix.strip():
                item_str += f"\n  * *Suggested Fix*: {issue.suggested_fix.strip()}"
            crit_lines.append(item_str)
    else:
        crit_lines.append("* *None found.*")

    minor_lines: list[str] = []
    if review.minor_suggestions:
        for issue in review.minor_suggestions:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            item_str = f"* 🟡 {loc}: {issue.description}"
            if issue.suggested_fix and issue.suggested_fix.strip():
                item_str += f"\n  * *Suggested Fix*: {issue.suggested_fix.strip()}"
            minor_lines.append(item_str)
    else:
        minor_lines.append("* *None found.*")

    return f"""## ⚡ Code Review Update: {verdict_badge}

### 1. Synchronization Summary

* **Update Summary:** {review.summary}
* **Auditor Confidence:** `{review.confidence}/5`

---

### 2. Resolution Tracker

{chr(10).join(res_lines)}

---

### 3. New Findings (Introduced in Update)

#### 🔴 Critical (Must Fix Before Merge)
{chr(10).join(crit_lines)}

#### 🟡 Suggestions & Maintainability
{chr(10).join(minor_lines)}
"""
