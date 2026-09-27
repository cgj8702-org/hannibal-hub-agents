"""Pydantic schemas for structured LLM review output in Webhook Receiver Agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def clean_field_string(val: Any) -> Any:
    """Universal string sanitizer that strips any leading Markdown keys, headers, badges, or bullet prefixes."""
    if not isinstance(val, str):
        return val
    s = val.strip()
    # Strips any leading bullets (* - • #), badges (🔴🟡✅), or key labels (**Any Key Name:**)
    cleaned = re.sub(
        r"^(?:(?:\*|-|•|#+|🔴|🟡|✅)\s*)*(?:\*\*)?[A-Za-z0-9 &\-_/]+:\*\*?\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return cleaned.strip("* -•` ")


class RiskItem(BaseModel):
    """Potential failure mode, concurrency boundary, or unhandled edge case."""

    risk: str = Field(
        description="Specific edge case or risk factor (e.g., rate limits, memory leaks, unhandled exceptions)"
    )
    recommendation: str = Field(
        default="", description="Recommended safeguard or mitigation strategy"
    )

    @field_validator("risk", "recommendation", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)


class IssueItem(BaseModel):
    """Actionable code issue or suggestion."""

    path: str = Field(description="File path related to issue")
    line: int | None = Field(default=None, description="Line number if applicable")
    description: str = Field(description="Clear, clinical explanation of the issue")
    suggested_fix: str = Field(
        default="", description="Actionable code fix or refactoring suggestion"
    )
    window: str = Field(
        default="", description="Diff rows the finding sits on, prefixed with line numbers"
    )
    verify_steps: str = Field(default="", description="Literal procedure to verify the issue")

    @field_validator("path", "description", "window", "verify_steps", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    @field_validator("suggested_fix", mode="before")
    @classmethod
    def sanitize_suggested_fix(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return str(v or "")
        s = v.strip("\r\n")
        s = re.sub(
            r"^(?:(?:\*|-|•)\s*)*(?:\*\*)?suggested[-_\s]*fix:\*\*?\s*",
            "",
            s,
            flags=re.IGNORECASE,
        )
        return s.rstrip()

    def to_markdown(self, prefix: str = "") -> str:
        """Render IssueItem into clean GitHub Markdown bullet point with code block formatting."""
        loc = f"`{self.path}:{self.line}`" if self.line else f"`{self.path}`"
        prefix_str = f"{prefix} " if prefix else ""
        item_str = f"* {prefix_str}{loc}: {self.description}"
        if self.suggested_fix and self.suggested_fix.strip():
            fix = self.suggested_fix.strip()
            if "```" in fix:
                indented = "\n".join(f"    {line}" for line in fix.splitlines())
                item_str += f"\n  * *Suggested Fix*:\n{indented}"
            elif "\n" in fix:
                indented = "\n".join(f"    {line}" for line in fix.splitlines())
                item_str += f"\n  * *Suggested Fix*:\n    ```\n{indented}\n    ```"
            else:
                if not (fix.startswith("`") and fix.endswith("`")):
                    fix = f"`{fix}`"
                item_str += f"\n  * *Suggested Fix*: {fix}"
        return item_str


class SyncResolutionItem(BaseModel):
    """Resolution status of a previously requested review item in incremental commit diff."""

    item_description: str = Field(description="Description of previously requested issue")
    status: Literal["RESOLVED", "UNRESOLVED"] = Field(
        description="Whether the issue is RESOLVED or UNRESOLVED"
    )
    evidence: str = Field(description="Line citation or diff evidence verifying resolution")

    @field_validator("item_description", "evidence", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)


class CodeReviewResponse(BaseModel):
    """Structured Pydantic model for full initial PR code reviews."""

    executive_summary: str = Field(
        description="1-2 sentences summarizing PR goal, overall quality, and verdict rationale"
    )
    verdict: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"] | None = Field(
        default=None,
        description="Optional explicit review verdict (APPROVE, REQUEST_CHANGES, COMMENT)",
    )
    confidence: int | None = Field(
        default=None, description="Optional legacy auditor confidence rating"
    )
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Blocking critical issues (syntax errors, security flaws, broken contracts)",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking actionable suggestions for refactoring, performance, or readability",
    )
    risks_and_edge_cases: list[RiskItem] = Field(
        default_factory=list,
        description="Key risks or edge cases identified during analysis",
    )
    context_gaps: list[str] = Field(
        default_factory=list,
        description="Missing context or empty list if fully understood",
    )

    @field_validator("executive_summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Autonomous PR code review report."

    def to_markdown(self, verdict: str | None = None) -> str:
        """Render CodeReviewResponse into clean, modern GitHub Markdown."""
        verdict_str = verdict or self.verdict or "COMMENT"
        verdict_badge = f"`{verdict_str}`"

        critical_lines: list[str] = []
        if self.critical_issues:
            for issue in self.critical_issues:
                critical_lines.append(issue.to_markdown())
        else:
            critical_lines.append("* *None found.*")

        minor_lines: list[str] = []
        if self.minor_suggestions:
            for suggestion in self.minor_suggestions:
                minor_lines.append(suggestion.to_markdown())
        else:
            minor_lines.append("* *None found.*")

        risk_lines: list[str] = []
        if self.risks_and_edge_cases:
            for item in self.risks_and_edge_cases:
                risk_lines.append(f"* **Risk:** {item.risk}")
                if item.recommendation and item.recommendation.strip():
                    risk_lines.append(f"  * *Recommendation*: {item.recommendation.strip()}")
            risk_block = "\n".join(risk_lines).strip()
        else:
            risk_block = "* *None identified for this PR scope.*"

        markdown_parts = [
            f"## 🛡️ Code Review: {verdict_badge}",
            "",
            "### 1. Executive Summary",
            "",
            f"* **Summary & Justification:** {self.executive_summary}",
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

        if self.context_gaps:
            gaps_str = ", ".join(self.context_gaps)
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


class SyncReviewResponse(BaseModel):
    """Structured Pydantic model for incremental PR synchronization re-reviews."""

    summary: str = Field(description="1-2 sentences summarizing incremental commit changes")
    verdict: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"] | None = Field(
        default=None,
        description="Optional explicit review verdict (APPROVE, REQUEST_CHANGES, COMMENT)",
    )
    resolutions: list[SyncResolutionItem] = Field(
        default_factory=list,
        description="Resolution status for all previously requested findings",
    )
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Critical or blocking issues introduced in this update",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking minor suggestions or maintainability notes introduced in this update",
    )
    confidence: int | None = Field(
        default=None, description="Optional legacy auditor confidence rating"
    )

    @field_validator("summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Pull request synchronization review update."

    def to_markdown(self, verdict: str | None = None, has_prior_reviews: bool = True) -> str:
        """Render SyncReviewResponse into clean, modern GitHub Markdown."""
        if not has_prior_reviews:
            cr_data = {
                "executive_summary": self.summary or "Autonomous PR code review report.",
                "confidence": self.confidence,
                "verdict": self.verdict or verdict,
                "critical_issues": [item.model_dump() for item in self.critical_issues],
                "minor_suggestions": [item.model_dump() for item in self.minor_suggestions],
                "risks_and_edge_cases": [],
            }
            cr_obj = CodeReviewResponse.model_validate(cr_data)
            return cr_obj.to_markdown(verdict)

        verdict_str = verdict or self.verdict or "COMMENT"
        verdict_badge = f"`{verdict_str}`"

        res_lines: list[str] = []
        if self.resolutions:
            for item in self.resolutions:
                icon = "✅" if item.status == "RESOLVED" else "🔴"
                res_lines.append(
                    f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
                )
        else:
            res_lines.append("* *No prior review items tracked.*")

        crit_lines: list[str] = []
        if self.critical_issues:
            for issue in self.critical_issues:
                crit_lines.append(issue.to_markdown(prefix="🔴"))
        else:
            crit_lines.append("* *None found.*")

        minor_lines: list[str] = []
        if self.minor_suggestions:
            for issue in self.minor_suggestions:
                minor_lines.append(issue.to_markdown(prefix="🟡"))
        else:
            minor_lines.append("* *None found.*")

        return f"""## ⚡ Code Review Update: {verdict_badge}

### 1. Synchronization Summary

* **Update Summary:** {self.summary}

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
