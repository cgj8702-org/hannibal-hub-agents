"""Pydantic structured output models for multi-agent PR auditing in Webhook Agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RiskItem(BaseModel):
    """Specific risk factor, concurrency boundary, security issue, or breaking change."""

    category: Literal[
        "concurrency", "memory", "security", "breaking_change", "none"
    ] = Field(description="Risk category classification")
    file: str | None = Field(default=None, description="File path associated with risk")
    line_range: str | None = Field(
        default=None, description="Line range or specific line citation (e.g. L45-L50)"
    )
    description: str = Field(description="Clinical explanation of identified risk")
    remediation: str = Field(
        description="Recommended safeguard or code mitigation strategy"
    )


class AuditVerdict(BaseModel):
    """Structured audit verdict produced by verdict_agent."""

    verdict: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"] = Field(
        description="Final review verdict for the Pull Request"
    )
    confidence: float = Field(
        ge=0.0, le=5.0, description="Auditor confidence score from 0.0 to 5.0"
    )
    pr_type: Literal["dev_docs", "minor_fix", "core_backend"] = Field(
        description="PR scope classification output from pr_router"
    )
    summary: str = Field(
        description="Executive summary of audit findings and code quality"
    )
    risks: list[RiskItem] = Field(
        default_factory=list,
        description="List of identified risks. May be empty [] for clean dev/docs PRs.",
    )
