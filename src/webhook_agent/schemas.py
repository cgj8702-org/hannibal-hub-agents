"""Pydantic schemas for structured LLM review output in Webhook Receiver Agent."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Scorecard(BaseModel):
    """Numerical ratings on a 1-5 scale for code quality categories."""

    correctness: int = Field(
        ge=1, le=5, description="1-5 rating for code correctness & logic"
    )
    security: int = Field(ge=1, le=5, description="1-5 rating for security & privacy")
    performance: int = Field(
        ge=1, le=5, description="1-5 rating for performance & scale"
    )
    readability: int = Field(
        ge=1, le=5, description="1-5 rating for readability & style"
    )
    test_coverage: int = Field(
        ge=1, le=5, description="1-5 rating for unit & integration test coverage"
    )


class ScorecardEvidence(BaseModel):
    """Specific evidence from code diff for each scorecard category."""

    correctness: str = Field(
        description="Specific diff evidence supporting correctness rating"
    )
    security: str = Field(
        description="Specific diff evidence supporting security rating"
    )
    performance: str = Field(
        description="Specific diff evidence supporting performance rating"
    )
    readability: str = Field(
        description="Specific diff evidence supporting readability rating"
    )
    test_coverage: str = Field(
        description="Specific diff evidence supporting test coverage rating"
    )


class RiskItem(BaseModel):
    """Potential failure mode, concurrency boundary, or unhandled edge case."""

    risk: str = Field(
        description="Specific edge case or risk factor (e.g., rate limits, memory leaks, unhandled exceptions)"
    )
    recommendation: str = Field(
        description="Recommended safeguard or mitigation strategy"
    )


class IssueItem(BaseModel):
    """Actionable code issue or suggestion."""

    path: str = Field(description="File path related to issue")
    line: int | None = Field(default=None, description="Line number if applicable")
    description: str = Field(description="Clear, clinical explanation of the issue")
    suggested_fix: str = Field(
        description="Actionable code fix or refactoring suggestion"
    )


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


class CodeReviewResponse(BaseModel):
    """Structured Pydantic model for full initial PR code reviews."""

    executive_summary: str = Field(
        description="1-2 sentences summarizing PR goal and overall quality"
    )
    scorecard: Scorecard
    scorecard_evidence: ScorecardEvidence
    confidence: int = Field(
        ge=1, le=5, description="Auditor confidence rating from 1 to 5"
    )
    risks_and_edge_cases: list[RiskItem] = Field(
        min_length=1,
        description="At least one risk or edge case item is required. Zero risks is unacceptable.",
    )
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Blocking critical issues (syntax errors, security flaws, broken contracts)",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking actionable suggestions for refactoring or readability",
    )
    context_gaps: list[str] = Field(
        default_factory=list,
        description="Missing context or empty list if fully understood",
    )


class SyncReviewResponse(BaseModel):
    """Structured Pydantic model for incremental PR synchronization re-reviews."""

    summary: str = Field(
        description="1-2 sentences summarizing incremental commit changes"
    )
    resolutions: list[SyncResolutionItem] = Field(
        description="Resolution status for all previously requested findings"
    )
    new_findings: list[IssueItem] = Field(
        default_factory=list,
        description="Any new issues introduced in the incremental commit update",
    )
    confidence: int = Field(
        ge=1, le=5, description="Auditor confidence rating from 1 to 5"
    )
