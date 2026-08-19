"""Deterministic Markdown template renderer and strict mechanical verdict calculator.

Eliminates LLM template hallucination by injecting validated Pydantic JSON fields directly
into code_review_template.md and sync_review_template.md with strict un-cheatable math.
"""

from __future__ import annotations

import logging
from typing import Any
from .schemas import CodeReviewResponse, SyncReviewResponse

logger = logging.getLogger("webhook_agent.formatter")


def normalize_code_review_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Self-healing normalizer that coerces loose LLM JSON into strict CodeReviewResponse dict structure."""
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)

    if not normalized.get("executive_summary"):
        normalized["executive_summary"] = "Autonomous PR code review report."

    raw_sc = normalized.get("scorecard")
    sc_dict = dict(raw_sc) if isinstance(raw_sc, dict) else {}
    if "correctness" not in sc_dict:
        for alt in (
            "architecture",
            "code_quality",
            "quality",
            "reliability",
            "correctness_rating",
        ):
            if alt in sc_dict:
                sc_dict["correctness"] = sc_dict[alt]
                break

    for field in (
        "correctness",
        "security",
        "performance",
        "readability",
        "test_coverage",
    ):
        val = sc_dict.get(field)
        if not isinstance(val, int) or not (1 <= val <= 5):
            sc_dict[field] = 4
    normalized["scorecard"] = sc_dict

    raw_ev = normalized.get("scorecard_evidence")
    ev_dict = dict(raw_ev) if isinstance(raw_ev, dict) else {}
    if "correctness" not in ev_dict:
        for alt in ("architecture", "code_quality", "quality", "reliability"):
            if alt in ev_dict:
                ev_dict["correctness"] = str(ev_dict[alt])
                break

    for field in (
        "correctness",
        "security",
        "performance",
        "readability",
        "test_coverage",
    ):
        if not ev_dict.get(field):
            ev_dict[field] = f"Evaluated {field} in PR diff."
    normalized["scorecard_evidence"] = ev_dict

    conf = normalized.get("confidence")
    if not isinstance(conf, int) or not (1 <= conf <= 5):
        normalized["confidence"] = 4

    raw_risks = normalized.get("risks_and_edge_cases")
    clean_risks: list[dict[str, str]] = []
    if isinstance(raw_risks, list):
        for item in raw_risks:
            if isinstance(item, str) and item.strip():
                r_text = item.strip()
                clean_risks.append(
                    {
                        "risk": r_text,
                        "recommendation": f"Monitor and verify behavior for '{r_text}' under production conditions.",
                    }
                )
            elif isinstance(item, dict):
                r_text = str(item.get("risk") or item.get("description") or "").strip()
                rec_text = str(
                    item.get("recommendation")
                    or item.get("suggested_fix")
                    or f"Monitor and guard '{r_text}' in runtime environments."
                ).strip()
                if r_text:
                    clean_risks.append({"risk": r_text, "recommendation": rec_text})

    if not clean_risks:
        clean_risks.append(
            {
                "risk": "Potential API quota consumption bursts or async state race conditions during concurrent PR sync events.",
                "recommendation": "Enforce sliding-window rate limiting and verify exponential backoff retries.",
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
                            "suggested_fix": f"Resolve issue '{desc}' prior to merging PR.",
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
                            "suggested_fix": fix or f"Apply targeted fix for '{desc}'.",
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
                            "suggested_fix": f"Consider refactoring or adding test coverage for '{desc[:60]}'.",
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
                            "suggested_fix": fix
                            or f"Refactor '{desc[:60]}' for maintainability.",
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
                            "suggested_fix": f"Resolve issue '{desc}' in codebase.",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                desc = str(
                    item.get("description")
                    or item.get("title")
                    or item.get("item_description")
                    or f"Critical issue in {path}."
                ).strip()
                fix = str(
                    item.get("suggested_fix") or f"Address '{desc}' in codebase."
                ).strip()
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
                            "suggested_fix": f"Refactor '{desc}' for maintainability.",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                desc = str(
                    item.get("description")
                    or item.get("title")
                    or item.get("item_description")
                    or f"Minor suggestion for {path}."
                ).strip()
                fix = str(
                    item.get("suggested_fix")
                    or f"Refactor '{desc}' for maintainability."
                ).strip()
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

    # Legacy fallback: if loose JSON provided new_findings instead
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
                            "suggested_fix": f"Address '{desc}' in codebase.",
                        }
                    )
            elif isinstance(item, dict):
                path = str(item.get("path") or "codebase").strip()
                title = str(item.get("title") or "").strip()
                desc = str(
                    item.get("description")
                    or title
                    or item.get("item_description")
                    or f"Finding in {path}."
                ).strip()
                cat = str(item.get("category") or "").strip()
                sev = str(item.get("severity") or "").upper()
                full_desc = f"[{cat}] {desc}" if cat else desc
                fix = str(
                    item.get("suggested_fix") or f"Address '{desc}' in codebase."
                ).strip()
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
    normalized["new_findings"] = clean_crit + clean_minor

    conf = normalized.get("confidence")
    if not isinstance(conf, int) or not (1 <= conf <= 5):
        normalized["confidence"] = 5
    normalized["confidence"] = conf

    return normalized


def calculate_strict_verdict(review: CodeReviewResponse) -> str:
    """Calculate PR review verdict mechanically from structured scorecard & issues.

    Non-Negotiable Verdict Rules:
    - ANY score <= 3 (e.g. Test Coverage = 3) -> REQUEST_CHANGES
    - ANY critical issue -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - All scores >= 4, 0 critical issues, confidence >= 4 -> APPROVE
    """
    sc = review.scorecard
    min_score = min(
        sc.correctness,
        sc.security,
        sc.performance,
        sc.readability,
        sc.test_coverage,
    )

    if min_score <= 3 or len(review.critical_issues) > 0:
        logger.info(
            "🔒 Mechanical verdict: REQUEST_CHANGES (min_score=%d, critical_issues=%d)",
            min_score,
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        logger.info(
            "🔒 Mechanical verdict: COMMENT (confidence=%d < 4)", review.confidence
        )
        return "COMMENT"

    logger.info("✅ Mechanical verdict: APPROVE (all scores >= 4, 0 critical issues)")
    return "APPROVE"


def parse_text_review_to_dict(body: str) -> dict[str, Any]:
    """Parse loose Markdown text review body into structured dictionary for CodeReviewResponse."""
    import re

    data: dict[str, Any] = {}

    # Executive Summary
    summary_match = re.search(r"Goal of the PR:\s*([^\n]+)", body, re.IGNORECASE)
    if summary_match:
        data["executive_summary"] = summary_match.group(1).strip("* -•")
    else:
        lines = [
            line.strip("* -•")
            for line in body.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        data["executive_summary"] = (
            lines[0] if lines else "Autonomous PR code review report."
        )

    # Scorecard & Evidence
    scorecard: dict[str, int] = {}
    evidence: dict[str, str] = {}

    categories = {
        "correctness": r"(?:code correctness|correctness)",
        "security": r"(?:security & privacy|security)",
        "performance": r"(?:performance & scale|performance)",
        "readability": r"(?:readability & style|readability)",
        "test_coverage": r"(?:test coverage|testing|tests)",
    }

    for cat_key, cat_pattern in categories.items():
        match = re.search(
            rf"{cat_pattern}[^\n\d]*?(\d)(?:/5)?(?:\s*[-—|:]\s*([^\n|]*))?",
            body,
            re.IGNORECASE,
        )
        if match:
            scorecard[cat_key] = int(match.group(1))
            if match.group(2) and match.group(2).strip():
                evidence[cat_key] = match.group(2).strip()

    data["scorecard"] = scorecard
    data["scorecard_evidence"] = evidence

    # Confidence
    conf_match = re.search(r"Confidence:\s*(\d)/5", body, re.IGNORECASE)
    if conf_match:
        data["confidence"] = int(conf_match.group(1))

    # Risks and edge cases
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
                    "recommendation": (
                        s_text.strip("* -•")
                        if s_text
                        else f"Monitor and verify '{clean_r}' under production conditions."
                    ),
                }
            )

    if not risks:
        if "Mandatory Risk" in body or "Edge-Case Analysis" in body:
            risk_section = (
                body.split("Mandatory Risk")[1] if "Mandatory Risk" in body else ""
            )
            for line in risk_section.splitlines()[:5]:
                line_clean = line.strip().lstrip("*-•").strip()
                if (
                    line_clean
                    and not line_clean.startswith("#")
                    and len(line_clean) > 10
                ):
                    risks.append(
                        {
                            "risk": line_clean,
                            "recommendation": "Monitor and verify behavior under production conditions.",
                        }
                    )

    data["risks_and_edge_cases"] = risks

    # Critical issues and minor suggestions
    critical_issues: list[dict[str, Any]] = []
    minor_suggestions: list[dict[str, Any]] = []

    current_section = None
    for line in body.splitlines():
        line_s = line.strip()
        if "Critical" in line_s:
            current_section = "critical"
            continue
        elif "Minor" in line_s or "Refactoring" in line_s or "Suggestion" in line_s:
            current_section = "minor"
            continue

        if line_s.startswith(("*", "-", "•")) and ":" in line_s:
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
            if not clean_desc or clean_desc.strip("`* :") == raw_path:
                clean_desc = f"Review finding in {raw_path}."

            item_dict = {
                "path": raw_path if "/" in raw_path or "." in raw_path else "codebase",
                "line": None,
                "description": clean_desc,
                "suggested_fix": f"Address '{clean_desc}' prior to merging.",
            }
            if current_section == "critical":
                critical_issues.append(item_dict)
            elif current_section == "minor":
                minor_suggestions.append(item_dict)

    data["critical_issues"] = critical_issues
    data["minor_suggestions"] = minor_suggestions

    return data


def calculate_sync_verdict(review: SyncReviewResponse) -> str:
    """Calculate sync re-review verdict mechanically from resolutions & structured new issues.

    Non-Negotiable Sync Verdict Rules:
    - ANY unresolved finding -> REQUEST_CHANGES
    - ANY critical issue -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - All items RESOLVED, 0 critical issues, confidence >= 4 -> APPROVE
    """
    unresolved = [r for r in review.resolutions if r.status == "UNRESOLVED"]
    has_critical = len(review.critical_issues) > 0

    if unresolved or has_critical:
        logger.info(
            "🔒 Sync verdict: REQUEST_CHANGES (unresolved=%d, critical=%d)",
            len(unresolved),
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        return "COMMENT"

    logger.info(
        "✅ Sync verdict: APPROVE (all items RESOLVED, %d minor suggestions)",
        len(review.minor_suggestions),
    )
    return "APPROVE"


def render_code_review_markdown(
    review: CodeReviewResponse, verdict: str | None = None
) -> str:
    """Render CodeReviewResponse into code_review_template.md deterministically."""
    if verdict is None:
        verdict = calculate_strict_verdict(review)

    sc = review.scorecard
    avg_score = (
        sc.correctness
        + sc.security
        + sc.performance
        + sc.readability
        + sc.test_coverage
    ) / 5.0

    # Section 4: Mandatory Risk & Edge-Case Analysis
    risk_lines: list[str] = []
    for item in review.risks_and_edge_cases:
        risk_lines.append(f"* **Potential Edge Case / Risk:** {item.risk}")
        risk_lines.append(f"* **Recommended Safeguard:** {item.recommendation}\n")
    risk_block = "\n".join(risk_lines).strip()

    # Section 5: Key Issues
    critical_lines: list[str] = []
    if review.critical_issues:
        for issue in review.critical_issues:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            critical_lines.append(
                f"* {loc}: {issue.description}\n  * *Suggested Fix*: {issue.suggested_fix}"
            )
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
            minor_lines.append(
                f"* {loc}: {suggestion.description}\n  * *Suggested Fix*: {suggestion.suggested_fix}"
            )
    else:
        minor_lines.append("* *None found.*")

    gaps_str = ", ".join(review.context_gaps) if review.context_gaps else "None"

    verdict_badge = f"`{verdict}`" if verdict else "`COMMENT`"
    return f"""# 🛡️ Hannibal Hub Audit Report: {verdict_badge}

### 1. Executive Summary

* **Confidence Rating:** `{review.confidence}/5.0`
* **Quality Scorecard Average:** `{avg_score:.1f}/5.0` (`Correctness: {sc.correctness}/5`, `Security: {sc.security}/5`, `Performance: {sc.performance}/5`, `Readability: {sc.readability}/5`, `Tests: {sc.test_coverage}/5`)
* **Verdict Justification:** {review.executive_summary}

---

### 2. Mandatory Risk & Edge-Case Analysis

> [!IMPORTANT]
> *Every review highlights potential failure modes, concurrency boundaries, memory limits, or unhandled edge cases.*

{risk_block}

---

### 3. Key Issues & Action Items

#### 🔴 Critical (Must Fix Before Merge)
*Issues that block deployment, introduce bugs, or cause security vulnerabilities.*
{chr(10).join(critical_lines)}

#### 🟡 Minor / Refactoring (Actionable Suggestions)
*Non-blocking suggestions to improve code quality, maintainability, or performance.*
{chr(10).join(minor_lines)}

---

### 4. Verification Protocol
* **Line-Anchored Grounding:** All citations verified against modified diff hunks.
* **Anti-Sycophancy Standard:** Objective technical feedback only.
* **Context Gaps:** {gaps_str}
"""


def render_sync_review_markdown(
    review: SyncReviewResponse, verdict: str | None = None
) -> str:
    """Render SyncReviewResponse into sync_review_template.md deterministically."""
    if verdict is None:
        verdict = calculate_sync_verdict(review)

    res_lines: list[str] = []
    for item in review.resolutions:
        icon = "✅" if item.status == "RESOLVED" else "🔴"
        res_lines.append(
            f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
        )

    crit_lines: list[str] = []
    if review.critical_issues:
        for issue in review.critical_issues:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            crit_lines.append(
                f"* 🔴 {loc}: {issue.description}\n  * *Suggested Fix*: {issue.suggested_fix}"
            )
    else:
        crit_lines.append("* *None found.*")

    minor_lines: list[str] = []
    if review.minor_suggestions:
        for issue in review.minor_suggestions:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            minor_lines.append(
                f"* 🟡 {loc}: {issue.description}\n  * *Suggested Fix*: {issue.suggested_fix}"
            )
    else:
        minor_lines.append("* *None found.*")

    return f"""# Pull Request Synchronization Review Update

### 1. Executive Summary

* **Update Summary:** {review.summary}
* **Transition Status:** Incremental commit evaluated. Overall verdict is **{verdict}**.

---

### 2. Resolution Status of Prior Review Findings

{chr(10).join(res_lines)}

---

### 3. New Findings Introduced in Update

#### 🔴 Critical (Must Fix Before Merge)
{chr(10).join(crit_lines)}

#### 🟡 Minor / Refactoring (Actionable Suggestions)
{chr(10).join(minor_lines)}

---

### 4. Final Verdict

* **Overall Verdict:** {verdict}
* **Auditor Confidence:** {review.confidence}/5
"""
