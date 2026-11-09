"""Payload normalization and object repair for LLM-generated code review data.

Decoupled from schemas.py to preserve declarative Pydantic schemas and ensure
pure, acyclic data modeling.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("webhook_agent.review.normalizer")


def clean_field_string(val: Any) -> Any:
    """Universal string sanitizer that strips any leading Markdown keys, headers, badges, or bullet prefixes.

    Safely preserves inline code backticks (e.g. `path/to/file.py`) without stripping opening backticks.
    """
    if val is None:
        return ""
    if not isinstance(val, str):
        return val
    s = val.strip()
    cleaned = re.sub(
        r"^(?:(?:\*|-|•|#+|🔴|🟡|✅|\[UNRESOLVED\]|\[RESOLVED\])\s*)*(?:(?:\*\*)?[A-Za-z0-9 &\-_/]+(?:\*\*:\s*|:\*\*\s*))?",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return cleaned.strip("* -•#\t\r\n ")


NOT_CHEAP_MARKERS = re.compile(
    r"\btrace\b|\bassum\w*\b|consider the case|if an attacker|"
    r"\bsimulat\w*\b|\bimagine\b|run the code|execute\b|another file|"
    r"\bgrep the repo\b|across (the )?(repo|codebase)",
    re.IGNORECASE,
)

MAX_BODY_CHARS = 600
MAX_UNBROKEN_RUN = 160
MAX_LINE_DIGITS = 12
MAX_LINE_NUMBER = 10**MAX_LINE_DIGITS


def parse_bounded_line_number(val: Any) -> int | None:
    """Safely coerce and bound line number to 1 <= line < 10**MAX_LINE_DIGITS."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or len(s) > MAX_LINE_DIGITS:
        return None
    try:
        iv = int(s)
        if 1 <= iv < MAX_LINE_NUMBER:
            return iv
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def bound_text_body(
    text: str,
    max_body_chars: int = MAX_BODY_CHARS,
    max_unbroken_run: int = MAX_UNBROKEN_RUN,
) -> str:
    """Bound text to prevent prompt injection payload dumping and essay dilation.

    Truncates any unbroken non-whitespace run exceeding max_unbroken_run (160)
    and caps total length to max_body_chars (600).
    """
    if not text:
        return ""
    parts = re.split(r"(\s+)", str(text))
    bounded_parts = [
        p[:max_unbroken_run] if not p.isspace() and len(p) > max_unbroken_run else p for p in parts
    ]
    bounded = "".join(bounded_parts)
    if len(bounded) > max_body_chars:
        bounded = bounded[:max_body_chars].rstrip()
    return bounded


def is_implausible_body(body: str) -> bool:
    """Check if a body string exceeds acceptable bounds for a code review comment.

    Enforces:
    - Total length <= MAX_BODY_CHARS (600 chars, ~100 words).
    - Longest unbroken non-whitespace run <= MAX_UNBROKEN_RUN (160 chars).
    """
    if len(body) > MAX_BODY_CHARS:
        return True
    longest = max((len(run) for run in body.split()), default=0)
    return longest > MAX_UNBROKEN_RUN


def is_not_cheap_finding(verify_steps: str) -> bool:
    """Check if stated verification steps admit needing multi-file tracing or speculative execution."""
    if not verify_steps:
        return False
    return bool(NOT_CHEAP_MARKERS.search(verify_steps))


def normalize_code_review_payload(data: Any) -> Any:
    """Normalize and repair raw dictionary payload before CodeReviewResponse validation."""
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
            clean_scorecard.append({"dimension": str(dim).strip(), "status": st, "notes": notes})
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
                line_val = parse_bounded_line_number(line_val)
                start_line_val = parse_bounded_line_number(start_line_val)

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
                            item.get("line_number") or item.get("lineNumber") or item.get("lineno")
                        )
                    l_val = parse_bounded_line_number(l_val)
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
                line_val = parse_bounded_line_number(line_val)
                start_line_val = parse_bounded_line_number(start_line_val)

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
                    item.get("invariant") or item.get("description") or item.get("evidence") or ""
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
                line_val = parse_bounded_line_number(line_val)
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
                    item.get("invariant") or item.get("description") or item.get("evidence") or ""
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


def normalize_sync_review_payload(data: Any) -> Any:
    """Normalize and repair raw dictionary payload before SyncReviewResponse validation."""
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
            clean_scorecard.append({"dimension": str(dim).strip(), "status": st, "notes": notes})
    normalized["scorecard"] = clean_scorecard

    raw_res = normalized.get("resolutions")
    clean_res: list[dict[str, str]] = []
    if isinstance(raw_res, list):
        for item in raw_res:
            if isinstance(item, BaseModel):
                item = item.model_dump()
            if isinstance(item, str) and item.strip():
                # Drop string-only resolutions without evidence grounding.
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
                if not desc or not ev:
                    continue
                raw_cat = str(item.get("category") or "CRITICAL").strip().upper()
                if "SUGG" in raw_cat or "MAINT" in raw_cat:
                    norm_cat = "SUGGESTION"
                elif "RISK" in raw_cat or "EDGE" in raw_cat:
                    norm_cat = "RISK"
                else:
                    norm_cat = "CRITICAL"
                item_path = str(
                    item.get("path")
                    or item.get("file_path")
                    or item.get("filename")
                    or item.get("filepath")
                    or ""
                ).strip()
                item_line = parse_bounded_line_number(
                    item.get("line") or item.get("line_number") or item.get("lineno")
                )
                res_dict: dict[str, Any] = {
                    "item_description": desc,
                    "status": status,
                    "evidence": ev,
                    "category": norm_cat,
                }
                if item_path:
                    res_dict["path"] = item_path
                if item_line is not None:
                    res_dict["line"] = item_line
                clean_res.append(res_dict)
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
                line_val = parse_bounded_line_number(line_val)
                start_line_val = parse_bounded_line_number(start_line_val)
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
                line_val = parse_bounded_line_number(line_val)
                start_line_val = parse_bounded_line_number(start_line_val)
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
                line_val = parse_bounded_line_number(line_val)
                start_line_val = parse_bounded_line_number(start_line_val)
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
                    item.get("invariant") or item.get("description") or item.get("evidence") or ""
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
                line_val = parse_bounded_line_number(line_val)
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
                    item.get("invariant") or item.get("description") or item.get("evidence") or ""
                ).strip()
                if inv_text:
                    clean_semantic.append(clean_field_string(inv_text))
    normalized["semantic_invariants"] = clean_semantic

    return normalized
