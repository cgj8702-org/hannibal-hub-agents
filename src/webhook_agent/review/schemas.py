"""Pydantic schemas for structured LLM review output in Webhook Receiver Agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .normalizer import (
    clean_field_string as clean_field_string,
)
from .normalizer import (
    is_implausible_body as is_implausible_body,
)
from .normalizer import (
    is_not_cheap_finding as is_not_cheap_finding,
)
from .normalizer import (
    normalize_code_review_payload,
    normalize_sync_review_payload,
)


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


class ScorecardItem(BaseModel):
    """Evaluation status and clinical notes for an audit dimension."""

    dimension: str = Field(
        description="Audit dimension (e.g. 'Logic & Boundaries', 'Concurrency & State', 'Security & Secrets', 'Contract Integrity', 'Test Coverage & Regressions')"
    )
    status: Literal["PASS", "WARN", "FAIL", "N/A", "UNKNOWN", "ERROR"] = Field(
        default="UNKNOWN",
        description="Audit dimension status: PASS, WARN, FAIL, N/A, UNKNOWN, or ERROR",
    )
    notes: str = Field(
        default="",
        description="Concrete technical evaluation notes, diff citations, or rationale",
    )

    @field_validator("dimension", "notes", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    @field_validator("status", mode="before")
    @classmethod
    def sanitize_status(cls, v: Any) -> str:
        if not v:
            return "UNKNOWN"
        s = str(v).strip().upper()
        if "FAIL" in s or "CRIT" in s or "BLOCK" in s:
            return "FAIL"
        if "ERR" in s or "CRASH" in s or "EXCEPTION" in s:
            return "ERROR"
        if "UNKNOWN" in s or "UNVERIF" in s or "PENDING" in s:
            return "UNKNOWN"
        if "WARN" in s or "CAUTION" in s:
            return "WARN"
        if "N/A" in s or "NA" in s or "SKIP" in s:
            return "N/A"
        if "PASS" in s or "OK" in s or "CLEAN" in s or "GOOD" in s:
            return "PASS"
        return "UNKNOWN"

    @model_validator(mode="after")
    def normalize_scorecard_notes(self) -> ScorecardItem:
        raw_notes = (self.notes or "").strip()
        upper_notes = raw_notes.upper()
        if not raw_notes:
            if self.status == "PASS":
                self.notes = "Verified clean."
            elif self.status == "N/A":
                self.notes = "Not applicable to modified diff scope."
        elif upper_notes in {"N/A", "NA", "NONE", "N / A", "NOT APPLICABLE", "NULL"}:
            if self.status == "PASS":
                self.notes = "Verified clean; not impacted by modified diff scope."
            elif self.status == "N/A":
                self.notes = "Not applicable to modified diff scope."
            elif self.status == "WARN":
                self.notes = "Potential risk identified in modified scope."
            elif self.status == "FAIL":
                self.notes = "Blocking defect identified in modified scope."
            elif self.status == "ERROR":
                self.notes = "Dimension evaluation error."
            else:
                self.notes = "No details provided."
        return self

    def to_table_row(self) -> str:
        """Render a clean Markdown table row."""
        badge_map = {
            "PASS": "`✅ PASS`",
            "WARN": "`⚠️ WARN`",
            "FAIL": "`❌ FAIL`",
            "N/A": "`⚪ N/A`",
            "UNKNOWN": "`❓ UNKNOWN`",
            "ERROR": "`💥 ERROR`",
        }
        badge = badge_map.get(self.status, f"`{self.status}`")
        raw_notes = (self.notes or "").strip()
        upper_notes = raw_notes.upper()
        if not raw_notes:
            safe_notes = (
                "Not applicable to modified diff scope."
                if self.status == "N/A"
                else "Verified clean."
            )
        elif upper_notes in {"N/A", "NA", "NONE", "N / A", "NOT APPLICABLE", "NULL"}:
            if self.status == "PASS":
                safe_notes = "Verified clean; not impacted by modified diff scope."
            elif self.status == "N/A":
                safe_notes = "Not applicable to modified diff scope."
            else:
                safe_notes = "No details provided."
        else:
            safe_notes = raw_notes.replace("|", "\\|")
        return f"| **{self.dimension}** | {badge} | {safe_notes} |"


class IssueItem(BaseModel):
    """Actionable code issue or suggestion."""

    path: str = Field(description="File path related to issue")
    line: int | None = Field(default=None, description="Line number if applicable")
    start_line: int | None = Field(
        default=None, description="Starting line number for multi-line suggestions"
    )
    description: str = Field(description="Clear, clinical explanation of the issue")
    suggested_fix: str = Field(
        default="", description="Actionable code fix or refactoring suggestion"
    )
    window: str = Field(
        default="", description="Diff rows the finding sits on, prefixed with line numbers"
    )
    verify_steps: str = Field(default="", description="Literal procedure to verify the issue")
    confidence_score: int | None = Field(default=None, description="Confidence score from 1 to 10")
    severity: str | None = Field(
        default=None, description="Finding severity (CRITICAL, HIGH, MEDIUM, LOW, NIT)"
    )
    category: str | None = Field(default=None, description="Optional issue category")

    @field_validator("path", "description", "window", "verify_steps", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        """Coerce common LLM alias fields (file_path -> path, line_number -> line)."""
        if isinstance(data, dict):
            if "path" not in data or not data["path"]:
                for key in ("file_path", "filename", "filepath"):
                    if data.get(key):
                        data["path"] = data[key]
                        break
            if "line" not in data or data["line"] is None:
                for key in ("line_number", "lineNumber", "lineno"):
                    if data.get(key) is not None:
                        try:
                            data["line"] = int(data[key])
                            break
                        except (ValueError, TypeError):
                            pass
            if "start_line" not in data or data["start_line"] is None:
                for key in ("start_line_number", "startLineNumber", "start_lineno"):
                    if data.get(key) is not None:
                        try:
                            data["start_line"] = int(data[key])
                            break
                        except (ValueError, TypeError):
                            pass
            if "confidence_score" not in data or data["confidence_score"] is None:
                for key in ("confidence", "confidenceScore", "score"):
                    if data.get(key) is not None:
                        try:
                            data["confidence_score"] = int(data[key])
                            break
                        except (ValueError, TypeError):
                            pass
        return data

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
        if not self.path:
            loc = "_(no file cited)_"
        else:
            loc = (
                f"`{self.path}:{self.start_line}-{self.line}`"
                if (self.start_line and self.line and self.start_line < self.line)
                else (f"`{self.path}:{self.line}`" if self.line else f"`{self.path}`")
            )
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


class CandidateFinding(BaseModel):
    """A candidate issue extracted during the high-recall Hunter stage."""

    id: str = Field(default="", description="Unique identifier for candidate tracking")
    file_path: str = Field(description="Target file path under review")
    line_number: int = Field(default=1, description="Line number where issue is situated")
    start_line: int | None = Field(default=None, description="Starting line if multi-line range")
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NIT"] = Field(
        default="MEDIUM", description="Assessed impact severity"
    )
    confidence_score: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Hunter confidence score from 1 (speculative) to 10 (certain)",
    )
    title: str = Field(description="Concise description/headline of the issue")
    description: str = Field(description="Detailed explanation of the flaw, bug, or risk")
    suggested_fix: str | None = Field(
        default=None, description="Actionable replacement code snippet or fix"
    )
    falsification_check: str = Field(
        default="",
        description="Explicit test or check for an adversarial critic to attempt to disprove this finding",
    )
    window: str | None = Field(
        default=None,
        description="Quoted code lines with line numbers as shown in the diff/file for verification",
    )
    verify_steps: str | None = Field(
        default=None,
        description="Stated procedure for verifying or reproducing this finding",
    )

    @field_validator(
        "file_path",
        "title",
        "description",
        "falsification_check",
        "window",
        "verify_steps",
        mode="before",
    )
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    def to_issue_item(self) -> IssueItem:
        """Convert verified CandidateFinding to an IssueItem for GitHub review submission."""
        return IssueItem(
            path=self.file_path,
            line=self.line_number,
            start_line=self.start_line,
            description=f"{self.title}: {self.description}" if self.title else self.description,
            suggested_fix=self.suggested_fix or "",
            confidence_score=self.confidence_score,
            severity=self.severity,
        )


class VerificationVerdict(BaseModel):
    """Adversarial verification result from the Critic stage."""

    finding_id: str = Field(description="ID of candidate finding being audited")
    verified: bool = Field(description="True if finding withstood adversarial falsification")
    confidence_score: int = Field(
        ge=1, le=10, description="Post-criticism confidence score from 1 to 10"
    )
    disproof_reason: str | None = Field(
        default=None,
        description="Explanation of why finding was disproven or determined to be a false-positive",
    )
    revised_description: str | None = Field(
        default=None, description="Refined description if finding is verified but adjusted"
    )
    revised_suggested_fix: str | None = Field(
        default=None, description="Refined replacement code snippet if applicable"
    )


class SyncResolutionItem(BaseModel):
    """Resolution status of a previously requested review item in incremental commit diff."""

    item_description: str = Field(description="Description of previously requested issue")
    status: Literal["RESOLVED", "UNRESOLVED"] = Field(
        description="Whether the issue is RESOLVED or UNRESOLVED"
    )
    evidence: str = Field(description="Line citation or diff evidence verifying resolution")
    category: Literal["CRITICAL", "SUGGESTION", "RISK"] = Field(
        default="CRITICAL",
        description="Category of the prior finding: CRITICAL, SUGGESTION, or RISK",
    )

    @field_validator("item_description", "evidence", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    @field_validator("category", mode="before")
    @classmethod
    def sanitize_category(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_upper = v.strip().upper()
            if "CRIT" in v_upper:
                return "CRITICAL"
            if "SUGG" in v_upper or "MAINT" in v_upper:
                return "SUGGESTION"
            if "RISK" in v_upper or "EDGE" in v_upper:
                return "RISK"
        return "CRITICAL"


class VerifiedInvariant(BaseModel):
    """A concrete invariant, boundary condition, or contract verified during audit."""

    invariant: str = Field(
        description="The specific invariant, contract, or boundary condition verified."
    )
    path: str = Field(description="Exact file path where the invariant is maintained or tested.")
    line: int = Field(description="Exact line number demonstrating invariant preservation.")
    evidence: str = Field(
        description="Concrete technical explanation or test citation proving preservation."
    )

    @field_validator("invariant", "path", "evidence", mode="before")
    @classmethod
    def sanitize_fields(cls, v: Any) -> Any:
        return clean_field_string(v)

    def to_markdown(self, prefix: str = "", **kwargs: Any) -> str:
        loc = f"`{self.path}:{self.line}`" if self.line else (f"`{self.path}`" if self.path else "")
        prefix_str = f"{prefix} " if prefix else ""
        if loc:
            head = f"* {prefix_str}**Invariant**: {self.invariant} ({loc})"
        else:
            head = f"* {prefix_str}**Invariant**: {self.invariant} (no citation provided)"
        evidence = self.evidence or "none provided"
        return f"{head}\n  *Evidence*: {evidence}"


def is_unmitigated_breaking_risk(risk: str, recommendation: str = "") -> bool:
    """Check if a risk text describes an active, unmitigated breaking risk.

    Deprecated: Natural language prose scraping in risks_and_edge_cases was deprecated to
    prevent brittle regex parsing and false-positive mechanical blocks. Reviewers and
    deterministic analyzers must explicitly place blocking issues in critical_issues or
    mark scorecard dimensions as FAIL. Always returns False.
    """
    return False


class CodeReviewResponse(BaseModel):
    """Structured Pydantic model for full initial PR code reviews."""

    executive_summary: str = Field(
        description="1-2 sentences summarizing PR goal, overall quality, and verdict rationale"
    )
    verdict: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"] | None = Field(
        default=None,
        description="Optional explicit review verdict (APPROVE, REQUEST_CHANGES, COMMENT)",
    )
    scorecard: list[ScorecardItem] = Field(
        default_factory=list,
        description="Systematic evaluation across mandatory audit dimensions",
    )
    candidate_findings: list[CandidateFinding] = Field(
        default_factory=list,
        description="High-recall candidate issues discovered before adversarial falsification",
    )
    verification_verdicts: list[VerificationVerdict] = Field(
        default_factory=list,
        description="Adversarial verification results from the Critic stage",
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
    verified_invariants: list[str] = Field(
        default_factory=list,
        description="Concrete, diff-grounded behavioral guarantees or invariants verified during audit",
    )
    semantic_invariants: list[str] = Field(
        default_factory=list,
        description="High-level architectural, domain, or behavioral guarantees asserted by the model without mechanical line proof",
    )
    unverified_claims: list[str] = Field(
        default_factory=list,
        description="Claims, residual uncertainty, or hypotheses that could not be fully verified against the diff.",
    )
    context_gaps: list[str] = Field(
        default_factory=list,
        description="Missing context or empty list if fully understood",
    )
    model: str | None = Field(
        default=None,
        description="Name of the model that conducted the review, for audit and provenance",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_dict_before(cls, data: Any) -> Any:
        return normalize_code_review_payload(data)

    @field_validator("executive_summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Autonomous PR code review report."

    def to_markdown(
        self,
        verdict: str | None = None,
        has_prior_reviews: bool = True,
        prefix: str = "",
        **kwargs: Any,
    ) -> str:
        """Render CodeReviewResponse into clean, modern GitHub Markdown."""
        from webhook_agent.review.formatter import render_code_review_markdown

        return render_code_review_markdown(self, verdict=verdict, **kwargs)


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
    scorecard: list[ScorecardItem] = Field(
        default_factory=list,
        description="Systematic evaluation across mandatory audit dimensions for incremental diff",
    )
    candidate_findings: list[CandidateFinding] = Field(
        default_factory=list,
        description="High-recall candidate issues discovered before adversarial falsification",
    )
    verification_verdicts: list[VerificationVerdict] = Field(
        default_factory=list,
        description="Adversarial verification results from the Critic stage",
    )
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Critical or blocking issues introduced in this update",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking minor suggestions or maintainability notes introduced in this update",
    )
    verified_invariants: list[str] = Field(
        default_factory=list,
        description="Concrete, diff-grounded behavioral guarantees or invariants verified during audit",
    )
    semantic_invariants: list[str] = Field(
        default_factory=list,
        description="High-level architectural, domain, or behavioral guarantees asserted by the model without mechanical line proof",
    )
    unverified_claims: list[str] = Field(
        default_factory=list,
        description="Claims, residual uncertainty, or hypotheses that could not be fully verified against the diff.",
    )
    model: str | None = Field(
        default=None,
        description="Name of the model that conducted the sync review, for audit and provenance",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_dict_before(cls, data: Any) -> Any:
        return normalize_sync_review_payload(data)

    @field_validator("summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Pull request synchronization review update."

    def to_markdown(
        self,
        verdict: str | None = None,
        has_prior_reviews: bool = True,
        prefix: str = "",
        **kwargs: Any,
    ) -> str:
        """Render SyncReviewResponse into clean, modern GitHub Markdown."""
        from webhook_agent.review.formatter import render_sync_review_markdown

        return render_sync_review_markdown(
            self, verdict=verdict, has_prior_reviews=has_prior_reviews, **kwargs
        )
