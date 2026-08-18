"""Deterministic Markdown template renderer and strict mechanical verdict calculator.

Eliminates LLM template hallucination by injecting validated Pydantic JSON fields directly
into code_review_template.md and sync_review_template.md with strict un-cheatable math.
"""

from __future__ import annotations

import logging
from .schemas import CodeReviewResponse, SyncReviewResponse

logger = logging.getLogger("webhook_agent.formatter")


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
    - ANY new finding -> REQUEST_CHANGES
    - Confidence < 4 -> COMMENT
    - All items RESOLVED, 0 new findings, confidence >= 4 -> APPROVE
    """
    unresolved = [r for r in review.resolutions if r.status == "UNRESOLVED"]
    if unresolved or len(review.new_findings) > 0:
        logger.info(
            "🔒 Sync verdict: REQUEST_CHANGES (unresolved=%d, new_findings=%d)",
            len(unresolved),
            len(review.new_findings),
        )
        return "REQUEST_CHANGES"

    if review.confidence < 4:
        return "COMMENT"

    logger.info("✅ Sync verdict: APPROVE (all items RESOLVED)")
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
                f"* **{loc}**: {issue.description}\n  * *Suggested Fix*: `{issue.suggested_fix}`"
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
                f"* **{loc}**: {suggestion.description}\n  * *Suggested Fix*: `{suggestion.suggested_fix}`"
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
                f"* 🔴 **{loc}**: {issue.description}\n  * *Suggested Fix*: `{issue.suggested_fix}`"
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
