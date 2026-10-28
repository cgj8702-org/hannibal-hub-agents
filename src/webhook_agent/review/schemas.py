"""Pydantic schemas for structured LLM review output in Webhook Receiver Agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def clean_field_string(val: Any) -> Any:
    """Universal string sanitizer that strips any leading Markdown keys, headers, badges, or bullet prefixes.

    Safely preserves inline code backticks (e.g. `path/to/file.py`) without stripping opening backticks.
    """
    if val is None:
        return ""
    if not isinstance(val, str):
        return val
    s = val.strip()
    # Strips any leading bullets (* - • #), badges (🔴🟡✅), status tags ([RESOLVED]),
    # or key labels (**Any Key Name:** or **Any Key Name**: )
    cleaned = re.sub(
        r"^(?:(?:\*|-|•|#+|🔴|🟡|✅|\[UNRESOLVED\]|\[RESOLVED\])\s*)*(?:(?:\*\*)?[A-Za-z0-9 &\-_/]+(?:\*\*:\s*|:\*\*\s*))?",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return cleaned.strip("* -•#\t\r\n ")


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
        safe_notes = self.notes.replace("|", "\\|").strip() or "Verified clean."
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

    @field_validator("file_path", "title", "description", "falsification_check", mode="before")
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

    def to_markdown(self) -> str:
        if self.path:
            loc = f"`{self.path}:{self.line}`" if self.line else f"`{self.path}`"
            head = f"* **Invariant**: {self.invariant} ({loc})"
        else:
            head = f"* **Invariant**: {self.invariant} (no citation provided)"
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


NOT_CHEAP_MARKERS = re.compile(
    r"\btrace\b|\bassum\w*\b|consider the case|if an attacker|"
    r"\bsimulat\w*\b|\bimagine\b|run the code|execute\b|another file|"
    r"\bgrep the repo\b|across (the )?(repo|codebase)",
    re.IGNORECASE,
)

MAX_BODY_CHARS = 4000
MAX_UNBROKEN_RUN = 120


def is_implausible_body(body: str) -> bool:
    """Check if a body string exceeds acceptable bounds for a code review comment."""
    if len(body) > MAX_BODY_CHARS:
        return True
    longest = max((len(run) for run in body.split()), default=0)
    return longest > MAX_UNBROKEN_RUN


def is_not_cheap_finding(verify_steps: str) -> bool:
    """Check if stated verification steps admit needing multi-file tracing or speculative execution."""
    if not verify_steps:
        return False
    return bool(NOT_CHEAP_MARKERS.search(verify_steps))


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
        if not isinstance(data, dict):
            return data
        normalized = dict(data)

        if normalized.get("verdict"):
            v_val = str(normalized["verdict"]).strip().upper()
            normalized["verdict"] = (
                v_val if v_val in ("APPROVE", "REQUEST_CHANGES", "COMMENT") else None
            )

        summary = normalized.get("executive_summary") or normalized.get("summary")
        if summary:
            cleaned = clean_field_string(summary)
            normalized["executive_summary"] = (
                cleaned if cleaned else "Autonomous PR code review report."
            )
        else:
            normalized["executive_summary"] = "Autonomous PR code review report."

        raw_scorecard = (
            normalized.get("scorecard")
            or normalized.get("audit_scorecard")
            or normalized.get("dimensions")
        )
        clean_scorecard: list[dict[str, Any]] = []
        if isinstance(raw_scorecard, list):
            for sc_item in raw_scorecard:
                if isinstance(sc_item, BaseModel):
                    sc_item = sc_item.model_dump()
                if isinstance(sc_item, dict):
                    dim = str(sc_item.get("dimension") or sc_item.get("name") or "").strip()
                    st = str(sc_item.get("status") or "UNKNOWN").strip()
                    notes = str(
                        sc_item.get("notes")
                        or sc_item.get("description")
                        or sc_item.get("evidence")
                        or ""
                    ).strip()
                    if dim:
                        clean_scorecard.append({"dimension": dim, "status": st, "notes": notes})
                elif isinstance(sc_item, str) and sc_item.strip():
                    if ":" in sc_item:
                        dim, notes = sc_item.split(":", 1)
                        clean_scorecard.append(
                            {"dimension": dim.strip(), "status": "UNKNOWN", "notes": notes.strip()}
                        )
                    else:
                        clean_scorecard.append(
                            {
                                "dimension": sc_item.strip(),
                                "status": "UNKNOWN",
                                "notes": "Unverified dimension.",
                            }
                        )
        elif isinstance(raw_scorecard, dict):
            for dim, val in raw_scorecard.items():
                if isinstance(val, dict):
                    st = str(val.get("status") or "UNKNOWN").strip()
                    notes = str(
                        val.get("notes") or val.get("description") or val.get("evidence") or ""
                    ).strip()
                else:
                    st = "UNKNOWN"
                    notes = str(val).strip()
                clean_scorecard.append(
                    {"dimension": str(dim).strip(), "status": st, "notes": notes}
                )
        normalized["scorecard"] = clean_scorecard

        raw_risks = normalized.get("risks_and_edge_cases") or normalized.get("risks")
        clean_risks: list[dict[str, str]] = []
        if isinstance(raw_risks, list):
            for item in raw_risks:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    r_text = item.strip()
                    if not any(
                        hdr in r_text.lower()
                        for hdr in (
                            "edge-case analysis",
                            "mandatory risk",
                            "section 4",
                            "scorecard summary",
                        )
                    ) and not r_text.startswith("#"):
                        clean_risks.append({"risk": r_text, "recommendation": ""})
                elif isinstance(item, dict):
                    r_text = str(item.get("risk") or item.get("description") or "").strip()
                    rec_text = str(
                        item.get("recommendation")
                        or item.get("suggested_fix")
                        or item.get("remediation")
                        or ""
                    ).strip()
                    if (
                        r_text
                        and not any(
                            hdr in r_text.lower()
                            for hdr in (
                                "edge-case analysis",
                                "mandatory risk",
                                "section 4",
                                "scorecard summary",
                            )
                        )
                        and not r_text.startswith("#")
                    ):
                        clean_risks.append({"risk": r_text, "recommendation": rec_text})
        normalized["risks_and_edge_cases"] = clean_risks

        raw_crit = normalized.get("critical_issues") or normalized.get("critical")
        clean_crit: list[dict[str, Any]] = []
        if isinstance(raw_crit, list):
            for item in raw_crit:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    desc = item.strip()
                    if desc.lower() not in ("none", "none found", "critical issue detected."):
                        clean_crit.append(
                            {
                                "path": "",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )

                elif isinstance(item, dict):
                    desc = str(item.get("description") or item.get("title") or "").strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    path_val = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    start_line_val = item.get("start_line")
                    if start_line_val is None:
                        start_line_val = (
                            item.get("start_line_number")
                            or item.get("startLineNumber")
                            or item.get("start_lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    try:
                        start_line_val = int(start_line_val) if start_line_val is not None else None
                    except (ValueError, TypeError):
                        start_line_val = None

                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in (
                        "none",
                        "none found",
                        "critical issue detected.",
                    ):
                        clean_crit.append(
                            {
                                "path": path_val,
                                "line": line_val,
                                "start_line": start_line_val,
                                "description": desc,
                                "suggested_fix": fix,
                                "window": window_val,
                                "verify_steps": steps,
                            }
                        )

        if isinstance(raw_risks, list):
            for item in raw_risks:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, dict):
                    cat = str(item.get("category") or "").strip().lower()
                    sev = str(item.get("severity") or "").strip().lower()
                    desc = str(item.get("description") or item.get("risk") or "").strip()
                    fix = str(
                        item.get("suggested_fix")
                        or item.get("recommendation")
                        or item.get("remediation")
                        or ""
                    ).strip()
                    if (
                        (
                            cat in ("breaking_change", "security", "critical", "blocker")
                            or sev in ("critical", "high", "blocker")
                        )
                        and desc
                        and not any(desc in c.get("description", "") for c in clean_crit)
                    ):
                        p_val = str(
                            item.get("path")
                            or item.get("file_path")
                            or item.get("filename")
                            or item.get("filepath")
                            or ""
                        ).strip()
                        l_val = item.get("line")
                        if l_val is None:
                            l_val = (
                                item.get("line_number")
                                or item.get("lineNumber")
                                or item.get("lineno")
                            )
                        try:
                            l_val = int(l_val) if l_val is not None else None
                        except (ValueError, TypeError):
                            l_val = None
                        clean_crit.append(
                            {
                                "path": p_val,
                                "line": l_val,
                                "description": desc,
                                "suggested_fix": fix,
                            }
                        )
        normalized["critical_issues"] = clean_crit

        raw_minor = normalized.get("minor_suggestions")
        clean_minor: list[dict[str, Any]] = []
        if isinstance(raw_minor, list):
            for item in raw_minor:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
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
                                "path": "",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )
                elif isinstance(item, dict):
                    desc = str(item.get("description") or item.get("title") or "").strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    path_val = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    start_line_val = item.get("start_line")
                    if start_line_val is None:
                        start_line_val = (
                            item.get("start_line_number")
                            or item.get("startLineNumber")
                            or item.get("start_lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    try:
                        start_line_val = int(start_line_val) if start_line_val is not None else None
                    except (ValueError, TypeError):
                        start_line_val = None

                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in (
                        "none",
                        "none found",
                        "minor suggestion.",
                        "minor suggestion",
                    ):
                        clean_minor.append(
                            {
                                "path": path_val,
                                "line": line_val,
                                "start_line": start_line_val,
                                "description": desc,
                                "suggested_fix": fix,
                                "window": window_val,
                                "verify_steps": steps,
                            }
                        )
        normalized["minor_suggestions"] = clean_minor

        raw_inv = normalized.get("verified_invariants") or normalized.get("invariants")
        clean_inv: list[str] = []
        if isinstance(raw_inv, list):
            for item in raw_inv:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    clean_inv.append(clean_field_string(item.strip()))
                elif isinstance(item, dict):
                    inv_text = str(
                        item.get("invariant")
                        or item.get("description")
                        or item.get("evidence")
                        or ""
                    ).strip()
                    path_val = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    if path_val and line_val:
                        clean_inv.append(f"{inv_text} (`{path_val}:{line_val}`)")
                    elif inv_text:
                        clean_inv.append(inv_text)
        normalized["verified_invariants"] = clean_inv

        raw_semantic = normalized.get("semantic_invariants") or normalized.get(
            "model_asserted_invariants"
        )
        clean_semantic: list[str] = []
        if isinstance(raw_semantic, list):
            for item in raw_semantic:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    clean_semantic.append(clean_field_string(item.strip()))
                elif isinstance(item, dict):
                    inv_text = str(
                        item.get("invariant")
                        or item.get("description")
                        or item.get("evidence")
                        or ""
                    ).strip()
                    if inv_text:
                        clean_semantic.append(clean_field_string(inv_text))
        normalized["semantic_invariants"] = clean_semantic

        raw_unverified = (
            normalized.get("unverified_claims")
            or normalized.get("residual_uncertainty")
            or normalized.get("uncertain_claims")
        )
        normalized["unverified_claims"] = (
            [str(u).strip() for u in raw_unverified if str(u).strip()]
            if isinstance(raw_unverified, list)
            else []
        )

        raw_gaps = normalized.get("context_gaps")
        normalized["context_gaps"] = (
            [str(g) for g in raw_gaps if g] if isinstance(raw_gaps, list) else []
        )

        return normalized

    @field_validator("executive_summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        cleaned = clean_field_string(v)
        return cleaned if cleaned else "Autonomous PR code review report."

    def to_markdown(self, verdict: str | None = None) -> str:
        """Render CodeReviewResponse into clean, modern GitHub Markdown."""
        verdict_str = verdict or self.verdict or "COMMENT"
        verdict_badge = f"`{verdict_str}`"

        markdown_parts = [
            f"## 🛡️ Code Review: {verdict_badge}",
            "",
            "### 1. Executive Summary",
            "",
            self.executive_summary,
        ]

        section_counter = 2

        if self.scorecard:
            scorecard_rows = [item.to_table_row() for item in self.scorecard]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Audit Dimension Scorecard",
                    "",
                    "| Dimension | Status | Notes |",
                    "| :--- | :---: | :--- |",
                    *scorecard_rows,
                ]
            )
            section_counter += 1

        if self.critical_issues or self.minor_suggestions:
            markdown_parts.extend(["", "---", "", f"### {section_counter}. Action Items"])
            if self.critical_issues:
                markdown_parts.extend(
                    [
                        "",
                        "#### 🔴 Critical (Must Fix Before Merge)",
                        "\n".join(issue.to_markdown() for issue in self.critical_issues),
                    ]
                )
            if self.minor_suggestions:
                markdown_parts.extend(
                    [
                        "",
                        "#### 🟡 Suggestions & Maintainability",
                        "\n".join(sugg.to_markdown() for sugg in self.minor_suggestions),
                    ]
                )
            section_counter += 1

        if self.risks_and_edge_cases:
            risk_lines: list[str] = []
            for item in self.risks_and_edge_cases:
                risk_lines.append(f"* **Risk:** {item.risk}")
                if item.recommendation and item.recommendation.strip():
                    risk_lines.append(f"  * *Recommendation*: {item.recommendation.strip()}")
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Potential Risks & Edge Cases",
                    "",
                    "\n".join(risk_lines),
                ]
            )
            section_counter += 1

        if self.verified_invariants:
            inv_lines = [f"- [x] {inv}" for inv in self.verified_invariants]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Verified Invariants",
                    "",
                    "\n".join(inv_lines),
                ]
            )
            section_counter += 1

        if self.semantic_invariants:
            sem_lines = [f"* {inv}" for inv in self.semantic_invariants]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Semantic Invariants (Model-Asserted)",
                    "",
                    "\n".join(sem_lines),
                ]
            )
            section_counter += 1

        if self.unverified_claims:
            u_lines = [f"* {claim}" for claim in self.unverified_claims]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "### ❓ Unverified Claims & Residual Uncertainty",
                    "",
                    "\n".join(u_lines),
                ]
            )

        if self.context_gaps:
            gaps_str = ", ".join(self.context_gaps)
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "### Verification Notes",
                    f"* **Context Gaps:** {gaps_str}",
                ]
            )

        from webhook_agent.review.metadata import serialize_review_metadata

        meta = {
            "version": 1,
            "type": "initial",
            "verdict": verdict_str,
            "critical_issues": [
                {
                    "path": issue.path,
                    "line": issue.line,
                    "start_line": issue.start_line,
                    "description": issue.description,
                    "suggested_fix": issue.suggested_fix,
                    "category": "CRITICAL",
                }
                for issue in self.critical_issues
            ],
            "minor_suggestions": [
                {
                    "path": sugg.path,
                    "line": sugg.line,
                    "start_line": sugg.start_line,
                    "description": sugg.description,
                    "suggested_fix": sugg.suggested_fix,
                    "category": "SUGGESTION",
                }
                for sugg in self.minor_suggestions
            ],
            "risks": [
                {
                    "risk": item.risk,
                    "recommendation": item.recommendation,
                    "category": "RISK",
                }
                for item in self.risks_and_edge_cases
            ],
            "unverified_claims": list(self.unverified_claims),
        }
        if self.scorecard:
            meta["scorecard"] = [
                {"dimension": s.dimension, "status": s.status, "notes": s.notes}
                for s in self.scorecard
            ]
        if self.verified_invariants:
            meta["verified_invariants"] = list(self.verified_invariants)
        if self.semantic_invariants:
            meta["semantic_invariants"] = list(self.semantic_invariants)
        if self.model:
            meta["model"] = self.model
        metadata_comment = serialize_review_metadata(meta)
        markdown_parts.append(f"\n{metadata_comment}")

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
    scorecard: list[ScorecardItem] = Field(
        default_factory=list,
        description="Systematic evaluation across mandatory audit dimensions for incremental diff",
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
        if not isinstance(data, dict):
            return data
        normalized = dict(data)

        if normalized.get("verdict"):
            v_val = str(normalized["verdict"]).strip().upper()
            normalized["verdict"] = (
                v_val if v_val in ("APPROVE", "REQUEST_CHANGES", "COMMENT") else None
            )

        summary = normalized.get("summary") or normalized.get("executive_summary")
        if summary:
            cleaned = clean_field_string(summary)
            normalized["summary"] = (
                cleaned if cleaned else "Pull request synchronization review update."
            )
        else:
            normalized["summary"] = "Pull request synchronization review update."

        raw_scorecard = (
            normalized.get("scorecard")
            or normalized.get("audit_scorecard")
            or normalized.get("dimensions")
        )
        clean_scorecard: list[dict[str, Any]] = []
        if isinstance(raw_scorecard, list):
            for sc_item in raw_scorecard:
                if isinstance(sc_item, BaseModel):
                    sc_item = sc_item.model_dump()
                if isinstance(sc_item, dict):
                    dim = str(sc_item.get("dimension") or sc_item.get("name") or "").strip()
                    st = str(sc_item.get("status") or "UNKNOWN").strip()
                    notes = str(
                        sc_item.get("notes")
                        or sc_item.get("description")
                        or sc_item.get("evidence")
                        or ""
                    ).strip()
                    if dim:
                        clean_scorecard.append({"dimension": dim, "status": st, "notes": notes})
                elif isinstance(sc_item, str) and sc_item.strip():
                    if ":" in sc_item:
                        dim, notes = sc_item.split(":", 1)
                        clean_scorecard.append(
                            {"dimension": dim.strip(), "status": "UNKNOWN", "notes": notes.strip()}
                        )
                    else:
                        clean_scorecard.append(
                            {
                                "dimension": sc_item.strip(),
                                "status": "UNKNOWN",
                                "notes": "Unverified dimension.",
                            }
                        )
        elif isinstance(raw_scorecard, dict):
            for dim, val in raw_scorecard.items():
                if isinstance(val, dict):
                    st = str(val.get("status") or "UNKNOWN").strip()
                    notes = str(
                        val.get("notes") or val.get("description") or val.get("evidence") or ""
                    ).strip()
                else:
                    st = "UNKNOWN"
                    notes = str(val).strip()
                clean_scorecard.append(
                    {"dimension": str(dim).strip(), "status": st, "notes": notes}
                )
        normalized["scorecard"] = clean_scorecard

        raw_res = normalized.get("resolutions")
        clean_res: list[dict[str, str]] = []
        if isinstance(raw_res, list):
            for item in raw_res:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    # Drop string-only resolutions without evidence grounding.
                    # Placeholder evidence ("Verified in ... diff") invents
                    # resolution claims with no file:line proof.
                    continue
                elif isinstance(item, dict):
                    desc = str(
                        item.get("item_description")
                        or item.get("issue")
                        or item.get("description")
                        or item.get("title")
                        or ""
                    ).strip()
                    status = str(item.get("status") or "RESOLVED").strip().upper()
                    if status not in ("RESOLVED", "UNRESOLVED"):
                        status = "RESOLVED"
                    ev = str(item.get("evidence") or item.get("details") or "").strip()
                    # Drop placeholder resolutions: no description or no evidence
                    # means the model invented a tracker row with no grounding.
                    if not desc or not ev:
                        continue
                    raw_cat = str(item.get("category") or "CRITICAL").strip().upper()
                    if "SUGG" in raw_cat or "MAINT" in raw_cat:
                        norm_cat = "SUGGESTION"
                    elif "RISK" in raw_cat or "EDGE" in raw_cat:
                        norm_cat = "RISK"
                    else:
                        norm_cat = "CRITICAL"
                    clean_res.append(
                        {
                            "item_description": desc,
                            "status": status,
                            "evidence": ev,
                            "category": norm_cat,
                        }
                    )
        normalized["resolutions"] = clean_res

        raw_crit = normalized.get("critical_issues") or normalized.get("new_critical_issues")
        clean_crit: list[dict[str, Any]] = []
        if isinstance(raw_crit, list):
            for item in raw_crit:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    desc = item.strip()
                    if desc.lower() not in ("none", "none found"):
                        clean_crit.append(
                            {
                                "path": "",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )
                elif isinstance(item, dict):
                    path = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    start_line_val = item.get("start_line")
                    if start_line_val is None:
                        start_line_val = (
                            item.get("start_line_number")
                            or item.get("startLineNumber")
                            or item.get("start_lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    try:
                        start_line_val = int(start_line_val) if start_line_val is not None else None
                    except (ValueError, TypeError):
                        start_line_val = None
                    desc = str(
                        item.get("description")
                        or item.get("title")
                        or item.get("item_description")
                        or ""
                    ).strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in ("none", "none found"):
                        clean_crit.append(
                            {
                                "path": path,
                                "line": line_val,
                                "start_line": start_line_val,
                                "description": desc,
                                "suggested_fix": fix,
                                "window": window_val,
                                "verify_steps": steps,
                            }
                        )

        raw_minor = normalized.get("minor_suggestions") or normalized.get("new_minor_suggestions")
        clean_minor: list[dict[str, Any]] = []
        if isinstance(raw_minor, list):
            for item in raw_minor:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    desc = item.strip()
                    if desc.lower() not in ("none", "none found"):
                        clean_minor.append(
                            {
                                "path": "",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )
                elif isinstance(item, dict):
                    path = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    start_line_val = item.get("start_line")
                    if start_line_val is None:
                        start_line_val = (
                            item.get("start_line_number")
                            or item.get("startLineNumber")
                            or item.get("start_lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    try:
                        start_line_val = int(start_line_val) if start_line_val is not None else None
                    except (ValueError, TypeError):
                        start_line_val = None
                    desc = str(
                        item.get("description")
                        or item.get("title")
                        or item.get("item_description")
                        or ""
                    ).strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in ("none", "none found"):
                        clean_minor.append(
                            {
                                "path": path,
                                "line": line_val,
                                "start_line": start_line_val,
                                "description": desc,
                                "suggested_fix": fix,
                                "window": window_val,
                                "verify_steps": steps,
                            }
                        )

        raw_new = normalized.get("new_findings")
        if isinstance(raw_new, list) and not clean_crit and not clean_minor:
            for item in raw_new:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    desc = item.strip()
                    if desc.lower() not in ("none", "none found"):
                        clean_crit.append(
                            {
                                "path": "",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )
                elif isinstance(item, dict):
                    path = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    start_line_val = item.get("start_line")
                    if start_line_val is None:
                        start_line_val = (
                            item.get("start_line_number")
                            or item.get("startLineNumber")
                            or item.get("start_lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    try:
                        start_line_val = int(start_line_val) if start_line_val is not None else None
                    except (ValueError, TypeError):
                        start_line_val = None
                    title = str(item.get("title") or "").strip()
                    desc = str(
                        item.get("description") or title or item.get("item_description") or ""
                    ).strip()
                    cat = str(item.get("category") or "").strip()
                    sev = str(item.get("severity") or "").upper()
                    full_desc = f"[{cat}] {desc}" if cat else desc
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    if desc and desc.lower() not in ("none", "none found"):
                        issue_dict = {
                            "path": path,
                            "line": line_val,
                            "start_line": start_line_val,
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

        raw_unverified = (
            normalized.get("unverified_claims")
            or normalized.get("residual_uncertainty")
            or normalized.get("uncertain_claims")
        )
        normalized["unverified_claims"] = (
            [str(u).strip() for u in raw_unverified if str(u).strip()]
            if isinstance(raw_unverified, list)
            else []
        )

        raw_inv = normalized.get("verified_invariants") or normalized.get("invariants")
        clean_inv: list[str] = []
        if isinstance(raw_inv, list):
            for item in raw_inv:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    clean_inv.append(clean_field_string(item.strip()))
                elif isinstance(item, dict):
                    inv_text = str(
                        item.get("invariant")
                        or item.get("description")
                        or item.get("evidence")
                        or ""
                    ).strip()
                    path_val = str(
                        item.get("path")
                        or item.get("file_path")
                        or item.get("filename")
                        or item.get("filepath")
                        or ""
                    ).strip()
                    line_val = item.get("line")
                    if line_val is None:
                        line_val = (
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    try:
                        line_val = int(line_val) if line_val is not None else None
                    except (ValueError, TypeError):
                        line_val = None
                    if path_val and line_val:
                        clean_inv.append(f"{inv_text} (`{path_val}:{line_val}`)")
        normalized["verified_invariants"] = clean_inv

        raw_semantic = normalized.get("semantic_invariants") or normalized.get(
            "model_asserted_invariants"
        )
        clean_semantic: list[str] = []
        if isinstance(raw_semantic, list):
            for item in raw_semantic:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, str) and item.strip():
                    clean_semantic.append(clean_field_string(item.strip()))
                elif isinstance(item, dict):
                    inv_text = str(
                        item.get("invariant")
                        or item.get("description")
                        or item.get("evidence")
                        or ""
                    ).strip()
                    if inv_text:
                        clean_semantic.append(clean_field_string(inv_text))
        normalized["semantic_invariants"] = clean_semantic

        return normalized

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
                "verdict": self.verdict or verdict,
                "scorecard": [s.model_dump() for s in self.scorecard],
                "critical_issues": [item.model_dump() for item in self.critical_issues],
                "minor_suggestions": [item.model_dump() for item in self.minor_suggestions],
                "risks_and_edge_cases": [],
                "verified_invariants": list(self.verified_invariants),
                "semantic_invariants": list(self.semantic_invariants),
                "unverified_claims": list(self.unverified_claims),
                "model": self.model,
            }
            cr_obj = CodeReviewResponse.model_validate(cr_data)
            return cr_obj.to_markdown(verdict)

        verdict_str = verdict or self.verdict or "COMMENT"
        verdict_badge = f"`{verdict_str}`"

        res_lines: list[str] = []
        if self.resolutions:
            crit_res = [
                r for r in self.resolutions if getattr(r, "category", "CRITICAL") == "CRITICAL"
            ]
            sugg_res = [
                r for r in self.resolutions if getattr(r, "category", "CRITICAL") == "SUGGESTION"
            ]
            risk_res = [r for r in self.resolutions if getattr(r, "category", "CRITICAL") == "RISK"]

            has_categories = any(
                getattr(r, "category", "CRITICAL") in ("SUGGESTION", "RISK")
                for r in self.resolutions
            )
            if not has_categories:
                for item in self.resolutions:
                    icon = "✅" if item.status == "RESOLVED" else "🔴"
                    res_lines.append(
                        f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
                    )
            else:
                if crit_res:
                    res_lines.append("#### 🔴 Critical Issues")
                    for item in crit_res:
                        icon = "✅" if item.status == "RESOLVED" else "🔴"
                        res_lines.append(
                            f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
                        )
                if sugg_res:
                    res_lines.append("#### 🟡 Suggestions & Maintainability")
                    for item in sugg_res:
                        icon = "✅" if item.status == "RESOLVED" else "🟡"
                        res_lines.append(
                            f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
                        )
                if risk_res:
                    res_lines.append("#### 🛡️ Risks & Edge Cases")
                    for item in risk_res:
                        icon = "✅" if item.status == "RESOLVED" else "🛡️"
                        res_lines.append(
                            f"* {icon} **[{item.status}]** {item.item_description}\n  * *Evidence*: {item.evidence}"
                        )
        crit_lines: list[str] = []
        if self.critical_issues:
            for issue in self.critical_issues:
                crit_lines.append(issue.to_markdown(prefix="🔴"))

        minor_lines: list[str] = []
        if self.minor_suggestions:
            for issue in self.minor_suggestions:
                minor_lines.append(issue.to_markdown(prefix="🟡"))

        markdown_parts = [
            f"## ⚡ Code Review Update: {verdict_badge}",
            "",
            "### 1. Synchronization Summary",
            "",
            self.summary,
        ]

        section_counter = 2

        if self.resolutions:
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Resolution Tracker",
                    "",
                    "\n".join(res_lines),
                ]
            )
            section_counter += 1

        if self.scorecard:
            scorecard_rows = [item.to_table_row() for item in self.scorecard]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Audit Dimension Scorecard",
                    "",
                    "| Dimension | Status | Notes |",
                    "| :--- | :---: | :--- |",
                    *scorecard_rows,
                ]
            )
            section_counter += 1

        if self.critical_issues or self.minor_suggestions:
            markdown_parts.extend(
                ["", "---", "", f"### {section_counter}. New Findings (Introduced in Update)"]
            )
            if self.critical_issues:
                markdown_parts.extend(
                    [
                        "",
                        "#### 🔴 Critical (Must Fix Before Merge)",
                        "\n".join(crit_lines),
                    ]
                )
            if self.minor_suggestions:
                markdown_parts.extend(
                    [
                        "",
                        "#### 🟡 Suggestions & Maintainability",
                        "\n".join(minor_lines),
                    ]
                )
            section_counter += 1

        if self.verified_invariants:
            inv_lines = [f"- [x] {inv}" for inv in self.verified_invariants]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Verified Invariants",
                    "",
                    "\n".join(inv_lines),
                ]
            )
            section_counter += 1

        if self.semantic_invariants:
            sem_lines = [f"* {inv}" for inv in self.semantic_invariants]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    f"### {section_counter}. Semantic Invariants (Model-Asserted)",
                    "",
                    "\n".join(sem_lines),
                ]
            )
            section_counter += 1

        if self.unverified_claims:
            u_lines = [f"* {claim}" for claim in self.unverified_claims]
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "### ❓ Unverified Claims & Residual Uncertainty",
                    "",
                    "\n".join(u_lines),
                ]
            )

        from webhook_agent.review.metadata import serialize_review_metadata

        meta = {
            "version": 1,
            "type": "sync",
            "verdict": verdict_str,
            "resolutions": [
                {
                    "item_description": item.item_description,
                    "status": item.status,
                    "evidence": item.evidence,
                    "category": getattr(item, "category", "CRITICAL"),
                }
                for item in self.resolutions
            ],
            "critical_issues": [
                {
                    "path": issue.path,
                    "line": issue.line,
                    "start_line": issue.start_line,
                    "description": issue.description,
                    "suggested_fix": issue.suggested_fix,
                    "category": "CRITICAL",
                }
                for issue in self.critical_issues
            ],
            "minor_suggestions": [
                {
                    "path": sugg.path,
                    "line": sugg.line,
                    "start_line": sugg.start_line,
                    "description": sugg.description,
                    "suggested_fix": sugg.suggested_fix,
                    "category": "SUGGESTION",
                }
                for sugg in self.minor_suggestions
            ],
            "unverified_claims": list(self.unverified_claims),
        }
        if self.scorecard:
            meta["scorecard"] = [
                {"dimension": s.dimension, "status": s.status, "notes": s.notes}
                for s in self.scorecard
            ]
        if self.verified_invariants:
            meta["verified_invariants"] = list(self.verified_invariants)
        if self.semantic_invariants:
            meta["semantic_invariants"] = list(self.semantic_invariants)
        if self.model:
            meta["model"] = self.model
        metadata_comment = serialize_review_metadata(meta)
        markdown_parts.append(f"\n{metadata_comment}")

        return "\n".join(markdown_parts) + "\n"
