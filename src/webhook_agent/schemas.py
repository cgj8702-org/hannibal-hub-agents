"""Pydantic schemas for structured LLM review output in Webhook Receiver Agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def clean_field_string(val: Any) -> Any:
    """Universal string sanitizer that strips any leading Markdown keys, headers, badges, or bullet prefixes."""
    if val is None:
        return ""
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
        loc = f"`{self.path}:{self.line}`" if self.line else f"`{self.path}`"
        return f"* **Invariant**: {self.invariant} ({loc})\n  *Evidence*: {self.evidence}"


BREAKING_RISK_KEYWORDS = (
    "unauthorized modification",
    "unintended modification",
    "lockfile corruption",
    "breaking change",
)

SUMMARY_RISK_KEYWORDS = (
    "unaddressed",
    "unresolved",
    "must fix",
    "blocking",
    *BREAKING_RISK_KEYWORDS,
)

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


def has_genuine_summary_risk(summary: str | None) -> bool:
    """Check if summary mentions blocking or unaddressed risks without negation or resolution."""
    # Summary scraping was deprecated to prevent false-positive critical issue synthesis.
    # Reviews must explicitly place blocking issues in critical_issues or mark resolutions as UNRESOLVED.
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
    verified_invariants: list[VerifiedInvariant] = Field(
        default_factory=list,
        description="Mandatory for APPROVE: At least one verified invariant, edge case, or contract preserved by the PR.",
    )
    context_gaps: list[str] = Field(
        default_factory=list,
        description="Missing context or empty list if fully understood",
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
                                "path": "codebase",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )

                elif isinstance(item, dict):
                    desc = str(item.get("description") or "").strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    path_val = str(item.get("path") or "codebase").strip()
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
                                "line": item.get("line")
                                if isinstance(item.get("line"), int)
                                else None,
                                "start_line": item.get("start_line")
                                if isinstance(item.get("start_line"), int)
                                else None,
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
                        clean_crit.append(
                            {
                                "path": str(item.get("path") or "codebase"),
                                "line": (
                                    item.get("line") if isinstance(item.get("line"), int) else None
                                ),
                                "description": desc,
                                "suggested_fix": fix,
                            }
                        )

        for r_item in clean_risks:
            r_lower = r_item["risk"].lower()
            rec_lower = r_item["recommendation"].lower()
            if any(kw in r_lower or kw in rec_lower for kw in BREAKING_RISK_KEYWORDS):
                if not any(r_item["risk"] in c.get("description", "") for c in clean_crit):
                    # Only promote when a concrete recommendation exists.
                    # Placeholder fixes invent action items with no grounding.
                    if not r_item["recommendation"]:
                        continue
                    clean_crit.append(
                        {
                            "path": "uv.lock"
                            if ("lock" in r_lower or "marker" in r_lower)
                            else "codebase",
                            "line": None,
                            "description": r_item["risk"],
                            "suggested_fix": r_item["recommendation"],
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
                                "path": "codebase",
                                "line": None,
                                "description": desc,
                                "suggested_fix": "",
                            }
                        )
                elif isinstance(item, dict):
                    desc = str(item.get("description") or "").strip()
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    path_val = str(item.get("path") or "codebase").strip()
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
                                "line": item.get("line")
                                if isinstance(item.get("line"), int)
                                else None,
                                "start_line": item.get("start_line")
                                if isinstance(item.get("start_line"), int)
                                else None,
                                "description": desc,
                                "suggested_fix": fix,
                                "window": window_val,
                                "verify_steps": steps,
                            }
                        )
        normalized["minor_suggestions"] = clean_minor

        raw_inv = normalized.get("verified_invariants") or normalized.get("invariants")
        clean_inv: list[dict[str, Any]] = []
        if isinstance(raw_inv, list):
            for item in raw_inv:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, dict):
                    inv_text = str(item.get("invariant") or item.get("description") or "").strip()
                    path_val = str(item.get("path") or "").strip()
                    line_val = item.get("line")
                    evid_val = str(item.get("evidence") or item.get("proof") or "").strip()
                    if inv_text:
                        clean_inv.append(
                            {
                                "invariant": inv_text,
                                "path": path_val,
                                "line": line_val if isinstance(line_val, int) else None,
                                "evidence": evid_val,
                            }
                        )
        normalized["verified_invariants"] = clean_inv

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

        if self.verified_invariants:
            inv_lines = [item.to_markdown() for item in self.verified_invariants]
            inv_block = "\n".join(inv_lines)
            markdown_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "### 🛡️ Verified Invariants & Edge Cases",
                    "",
                    inv_block,
                ]
            )

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
        }
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
    critical_issues: list[IssueItem] = Field(
        default_factory=list,
        description="Critical or blocking issues introduced in this update",
    )
    minor_suggestions: list[IssueItem] = Field(
        default_factory=list,
        description="Non-blocking minor suggestions or maintainability notes introduced in this update",
    )
    verified_invariants: list[VerifiedInvariant] = Field(
        default_factory=list,
        description="Mandatory for APPROVE: At least one verified invariant, edge case, or contract preserved by the PR.",
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
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in ("none", "none found"):
                        clean_crit.append(
                            {
                                "path": path,
                                "line": item.get("line")
                                if isinstance(item.get("line"), int)
                                else None,
                                "start_line": item.get("start_line")
                                if isinstance(item.get("start_line"), int)
                                else None,
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
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    steps = str(item.get("verify_steps") or "").strip()
                    window_val = str(item.get("window") or "").strip()

                    if is_not_cheap_finding(steps) or is_implausible_body(desc):
                        continue
                    if desc and desc.lower() not in ("none", "none found"):
                        clean_minor.append(
                            {
                                "path": path,
                                "line": item.get("line")
                                if isinstance(item.get("line"), int)
                                else None,
                                "start_line": item.get("start_line")
                                if isinstance(item.get("start_line"), int)
                                else None,
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
                        item.get("description") or title or item.get("item_description") or ""
                    ).strip()
                    cat = str(item.get("category") or "").strip()
                    sev = str(item.get("severity") or "").upper()
                    full_desc = f"[{cat}] {desc}" if cat else desc
                    fix = str(item.get("suggested_fix") or "").strip("\r\n").rstrip()
                    if desc and desc.lower() not in ("none", "none found"):
                        issue_dict = {
                            "path": path,
                            "line": item.get("line") if isinstance(item.get("line"), int) else None,
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

        raw_inv = normalized.get("verified_invariants") or normalized.get("invariants")
        clean_inv: list[dict[str, Any]] = []
        if isinstance(raw_inv, list):
            for item in raw_inv:
                if isinstance(item, BaseModel):
                    item = item.model_dump()
                if isinstance(item, dict):
                    inv_text = str(item.get("invariant") or item.get("description") or "").strip()
                    path_val = str(item.get("path") or "").strip()
                    line_val = item.get("line")
                    evid_val = str(item.get("evidence") or item.get("proof") or "").strip()
                    if inv_text:
                        clean_inv.append(
                            {
                                "invariant": inv_text,
                                "path": path_val,
                                "line": line_val if isinstance(line_val, int) else None,
                                "evidence": evid_val,
                            }
                        )
        normalized["verified_invariants"] = clean_inv

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
                "critical_issues": [item.model_dump() for item in self.critical_issues],
                "minor_suggestions": [item.model_dump() for item in self.minor_suggestions],
                "risks_and_edge_cases": [],
                "verified_invariants": [item.model_dump() for item in self.verified_invariants],
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

        inv_section = ""
        if self.verified_invariants:
            inv_lines = [item.to_markdown() for item in self.verified_invariants]
            inv_section = (
                f"\n---\n\n### 🛡️ Verified Invariants & Edge Cases\n\n{chr(10).join(inv_lines)}\n"
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
        }
        metadata_comment = serialize_review_metadata(meta)

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
{inv_section}
{metadata_comment}
"""
