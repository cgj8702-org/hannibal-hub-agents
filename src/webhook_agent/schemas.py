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

    @field_validator("path", "description", "suggested_fix", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)


class SyncResolutionItem(BaseModel):
    """Resolution status of a previously requested review item in incremental commit diff."""

    item_description: str = Field(
        description="Description of previously requested issue"
    )
    status: Literal["RESOLVED", "UNRESOLVED"] = Field(
        description="Whether the issue is RESOLVED or UNRESOLVED"
    )
    evidence: str = Field(
        description="Line citation or diff evidence verifying resolution"
    )

    @field_validator("item_description", "evidence", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)


class CodeReviewResponse(BaseModel):
    """Structured Pydantic model for full initial PR code reviews."""

    executive_summary: str = Field(
        description="1-2 sentences summarizing PR goal, overall quality, and verdict rationale"
    )
    confidence: int = Field(
        ge=1, le=5, description="Auditor confidence rating from 1 to 5"
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


class SyncReviewResponse(BaseModel):
    """Structured Pydantic model for incremental PR synchronization re-reviews."""

    summary: str = Field(
        description="1-2 sentences summarizing incremental commit changes"
    )
    resolutions: list[SyncResolutionItem] = Field(
        description="Resolution status for all previously requested findings"
    )
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Critical or blocking issues introduced in this update",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking minor suggestions or maintainability notes introduced in this update",
    )
    confidence: int = Field(
        ge=1, le=5, description="Auditor confidence rating from 1 to 5"
    )

    @field_validator("summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Pull request synchronization review update."
