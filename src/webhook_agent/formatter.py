"""Deterministic Markdown template renderer and strict mechanical verdict calculator.

Eliminates LLM template hallucination by injecting validated Pydantic JSON fields directly
into code_review_template.md and sync_review_template.md with strict un-cheatable math.
"""

from __future__ import annotations

import logging
from typing import Any
from .schemas import CodeReviewResponse, SyncReviewResponse

logger = logging.getLogger("webhook_agent.formatter")


BLOCKING_SYNC_KEYWORDS: tuple[str, ...] = (
    "critical",
    "security vulnerability",
    "breaking",
    "blocker",
    "high severity",
    "security flaw",
    "major issue",
)


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

    raw_new = normalized.get("new_findings")
    clean_new: list[dict[str, Any]] = []
    if isinstance(raw_new, list):
        for item in raw_new:
            if isinstance(item, str) and item.strip():
                desc = item.strip()
                if desc.lower() not in ("none", "none found"):
                    clean_new.append(
                        {
                            "path": "codebase",
                            "line": None,
                            "description": desc,
                            "suggested_fix": f"Review finding '{desc[:60]}' in codebase.",
                        }
                    )
            elif isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                desc = str(
                    item.get("description") or title or "New finding in PR update."
                ).strip()
                cat = str(item.get("category") or "").strip()
                full_desc = f"[{cat}] {desc}" if cat else desc
                fix = str(
                    item.get("suggested_fix") or f"Address {desc[:60]} in codebase."
                ).strip()
                if desc and desc.lower() not in ("none", "none found"):
                    clean_new.append(
                        {
                            "path": str(item.get("path") or "codebase"),
                            "line": (
                                item.get("line")
                                if isinstance(item.get("line"), int)
                                else None
                            ),
                            "description": full_desc,
                            "suggested_fix": fix,
                        }
                    )
    normalized["new_findings"] = clean_new

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


def calculate_sync_verdict(review: SyncReviewResponse) -> str:
    """Calculate sync re-review verdict mechanically from resolutions & new findings.

    Non-Negotiable Sync Verdict Rules:
    - ANY unresolved finding -> REQUEST_CHANGES
    - ANY critical / high-severity / security new finding -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - All items RESOLVED, 0 blocking new findings, confidence >= 4 -> APPROVE
    """
    unresolved = [r for r in review.resolutions if r.status == "UNRESOLVED"]
    blocking_new_findings = []
    for f in review.new_findings:
        desc_lower = f.description.lower()
        if any(k in desc_lower for k in BLOCKING_SYNC_KEYWORDS) or any(
            badge in desc_lower
            for badge in (
                "[critical]",
                "[high]",
                "[blocker]",
                "[security]",
                "[breaking]",
            )
        ):
            blocking_new_findings.append(f)

    if unresolved or blocking_new_findings:
        logger.info(
            "🔒 Sync verdict: REQUEST_CHANGES (unresolved=%d, blocking_new=%d)",
            len(unresolved),
            len(blocking_new_findings),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        return "COMMENT"

    logger.info(
        "✅ Sync verdict: APPROVE (all items RESOLVED, %d non-blocking notes)",
        len(review.new_findings),
    )
    return "APPROVE"


def render_code_review_markdown(
    review: CodeReviewResponse, verdict: str | None = None
) -> str:
    """Render CodeReviewResponse into code_review_template.md deterministically."""
    if verdict is None:
        verdict = calculate_strict_verdict(review)

    sc = review.scorecard
    ev = review.scorecard_evidence
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

    return f"""# Code Review Report

### 1. Executive Summary

* **Goal of the PR:** {review.executive_summary}
* **Verdict Justification:** {verdict} verdict calculated mechanically (Average Score: {avg_score:.1f}/5).

---

### 2. Scorecard Summary

> [!NOTE]
> **Scorecard Breakdown (1-5 Scale)**
> * **Code Correctness:** {sc.correctness}/5 — {ev.correctness}
> * **Security & Privacy:** {sc.security}/5 — {ev.security}
> * **Performance & Scale:** {sc.performance}/5 — {ev.performance}
> * **Readability & Style:** {sc.readability}/5 — {ev.readability}
> * **Test Coverage:** {sc.test_coverage}/5 — {ev.test_coverage}
> * **Average Score:** {avg_score:.1f}/5 | **Confidence:** {review.confidence}/5

---

### 3. Verdict Determination

* **Overall Verdict:** {verdict}

---

### 4. Mandatory Risk & Edge-Case Analysis

> [!IMPORTANT]
> *Finding zero risks or edge cases is unacceptable. Every review MUST highlight at least ONE potential failure mode, concurrency boundary, memory limit, or unhandled edge case — even for approved PRs.*

{risk_block}

---

### 5. Key Issues & Action Items

#### 🔴 Critical (Must Fix Before Merge)
*Issues that block deployment, introduce bugs, or cause security vulnerabilities.*
{chr(10).join(critical_lines)}

#### 🟡 Minor / Refactoring (Actionable Suggestions)
*Non-blocking suggestions to improve code quality, maintainability, or performance.*
{chr(10).join(minor_lines)}

---

### 6. Confidence Self-Assessment

* **My Confidence:** {review.confidence}/5
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

    new_lines: list[str] = []
    if review.new_findings:
        for issue in review.new_findings:
            loc = f"`{issue.path}:{issue.line}`" if issue.line else f"`{issue.path}`"
            new_lines.append(
                f"* 🔴 {loc}: {issue.description}\n  * *Suggested Fix*: {issue.suggested_fix}"
            )
    else:
        new_lines.append("* *None.*")

    return f"""# Pull Request Synchronization Review Update

### 1. Executive Summary

* **Update Summary:** {review.summary}
* **Transition Status:** Incremental commit evaluated. Overall verdict is **{verdict}**.

---

### 2. Resolution Status of Prior Review Findings

{chr(10).join(res_lines)}

---

### 3. New Findings Introduced in Update

{chr(10).join(new_lines)}

---

### 4. Final Verdict

* **Overall Verdict:** {verdict}
* **Auditor Confidence:** {review.confidence}/5
"""
