"""Deterministic Markdown template renderer and strict mechanical verdict calculator.

Delegates schema normalization and markdown rendering natively to Pydantic schemas in `schemas.py`.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Generator
from typing import Any

from .schemas import (
    CodeReviewResponse,
    SyncReviewResponse,
)
from .schemas import (
    is_implausible_body as is_implausible_body,
)
from .schemas import (
    is_not_cheap_finding as is_not_cheap_finding,
)

logger = logging.getLogger("webhook_agent.review.formatter")

FINDING_KEYS = (
    "path|line|body|window|verify_steps|description|suggested_fix|executive_summary|"
    "risk|recommendation|summary|resolutions|critical_issues|minor_suggestions|"
    "risks_and_edge_cases|context_gaps|verdict|status|evidence|item_description|title|"
    "verified_invariants|invariant|unverified_claims|residual_uncertainty|"
    "semantic_invariants|model_asserted_invariants|"
    "scorecard|dimension|notes"
)

STRING_FIELD_OPEN = re.compile(rf'"(?:{FINDING_KEYS})"\s*:\s*"')
STRING_FIELD_END = re.compile(rf'"(?=\s*(?:,\s*"(?:{FINDING_KEYS})"\s*:|\s*}}))')
MAX_ENDS_PER_FIELD = 8
MAX_REPAIR_STEPS = 16384
MAX_REPAIR_FIELDS = 150


def _escape_value(value: str) -> str:
    """Re-escape a value's double quotes, normalizing first so it is idempotent."""
    return value.replace('\\"', '"').replace('"', '\\"')


def _repair_readings(block: str, budget: list[int]) -> Generator[str]:
    """Every way of escaping the block, one per choice of where values end."""
    stack: list[tuple[int, str]] = [(0, "")]
    while stack:
        if budget[0] <= 0:
            return
        index, prefix = stack.pop()
        opener = STRING_FIELD_OPEN.search(block, index)
        if opener is None:
            budget[0] -= 1
            yield prefix + block[index:]
            continue
        head = prefix + block[index : opener.end()]
        ends = list(
            itertools.islice(STRING_FIELD_END.finditer(block, opener.end()), MAX_ENDS_PER_FIELD)
        )
        if not ends:
            budget[0] -= 1
            yield head + block[opener.end() :]
            continue
        for end in reversed(ends):
            budget[0] -= 1
            value = block[opener.end() : end.start()]
            stack.append((end.end(), f'{head}{_escape_value(value)}"'))


def _repaired_findings(block: str) -> Any | None:
    """Attempt repair of malformed JSON strings caused by unescaped quotes."""
    fields = sum(1 for _ in STRING_FIELD_OPEN.finditer(block))
    if fields > MAX_REPAIR_FIELDS:
        return None
    readings: dict[str, Any] = {}
    decoder = json.JSONDecoder()
    budget = [MAX_REPAIR_STEPS]
    for candidate in _repair_readings(block, budget):
        try:
            start_bracket = min(
                [pos for pos in (candidate.find("{"), candidate.find("[")) if pos != -1],
                default=-1,
            )
            if start_bracket == -1:
                continue
            obj, _ = decoder.raw_decode(candidate, start_bracket)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, (dict, list)) and obj:
            readings.setdefault(json.dumps(obj, sort_keys=True), obj)
            if len(readings) > 1:
                return None
    if budget[0] <= 0 or len(readings) != 1:
        return None
    return next(iter(readings.values()))


def _salvage_objects(block: str) -> list[dict[str, Any]]:
    """Decode dictionary objects one by one, skipping ones that will not parse."""
    decoder = json.JSONDecoder()
    salvaged: list[dict[str, Any]] = []
    index = 0
    while (start := block.find("{", index)) != -1:
        try:
            candidate, index = decoder.raw_decode(block, start)
        except (ValueError, json.JSONDecodeError):
            index = start + 1
            continue
        if isinstance(candidate, dict) and any(
            k in candidate
            for k in (
                "path",
                "description",
                "risk",
                "summary",
                "item_description",
                "critical_issues",
                "resolutions",
                "candidate_findings",
            )
        ):
            salvaged.append(candidate)
    return salvaged


def extract_json_payload(text: str) -> dict[str, Any] | None:
    """Extract, repair, or salvage structured JSON payload from reviewer output."""
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    fenced_blocks = list(re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text))
    candidate_blocks: list[str] = []
    if fenced_blocks:
        candidate_blocks.append(fenced_blocks[-1].group(1).strip())
    if (cleaned.startswith("{") and cleaned.endswith("}")) or (
        cleaned.startswith("[") and cleaned.endswith("]")
    ):
        candidate_blocks.append(cleaned)
    if len(fenced_blocks) > 1:
        for fb in reversed(fenced_blocks[:-1]):
            candidate_blocks.append(fb.group(1).strip())
    for match in re.finditer(
        r"(\{[\s\S]*?\"(?:executive_summary|resolutions|critical_issues|minor_suggestions|summary|risks)\"[\s\S]*?\})",
        text,
    ):
        candidate_blocks.append(match.group(1).strip())
    for block in candidate_blocks:
        if not block:
            continue
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"critical_issues": parsed}
        except (ValueError, json.JSONDecodeError):
            pass

        # Trailing comma repair: strip trailing commas before closing brackets or braces
        clean_block = re.sub(r",\s*([\]\}])", r"\1", block)
        if clean_block != block:
            try:
                parsed = json.loads(clean_block)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {"critical_issues": parsed}
            except (ValueError, json.JSONDecodeError):
                pass

        repaired = _repaired_findings(block)
        if repaired is not None:
            if isinstance(repaired, dict):
                if any(
                    k in repaired
                    for k in (
                        "executive_summary",
                        "summary",
                        "scorecard",
                        "critical_issues",
                        "minor_suggestions",
                        "resolutions",
                        "verdict",
                    )
                ):
                    logger.info("Successfully repaired malformed JSON review object")
                    return repaired
            elif isinstance(repaired, list):
                logger.info("Successfully repaired malformed JSON review list")
                return {"critical_issues": repaired}
        salvaged = _salvage_objects(block)
        if salvaged:
            for obj in salvaged:
                if any(k in obj for k in ("critical_issues", "resolutions", "executive_summary")):
                    logger.info("Salvaged top-level review response object from malformed JSON")
                    return obj
            logger.info("Salvaged %d individual review items from malformed JSON", len(salvaged))
            return {"critical_issues": salvaged}
    return None


def truncate_log_payload(val: Any, max_length: int = 300) -> str:
    """Safely format and truncate tool/agent payloads for readable Cloud Logging output."""
    if val is None:
        return ""
    text = str(val)
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... [truncated {len(text) - max_length} chars]"


def normalize_code_review_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce loose LLM JSON into strict CodeReviewResponse dict structure via Pydantic model validation."""
    if not isinstance(data, dict):
        return {}
    res = CodeReviewResponse.model_validate(data).model_dump()
    if "verdict" not in data:
        res.pop("verdict", None)
    if "model" not in data:
        res.pop("model", None)
    return res


def normalize_sync_review_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce loose LLM sync review JSON into strict SyncReviewResponse dict structure via Pydantic model validation."""
    if not isinstance(data, dict):
        return {}
    res = SyncReviewResponse.model_validate(data).model_dump()
    if "verdict" not in data:
        res.pop("verdict", None)
    if "model" not in data:
        res.pop("model", None)
    return res


def calculate_strict_verdict(review: CodeReviewResponse) -> str:
    """Calculate PR review verdict mechanically from structured issues."""
    if len(review.critical_issues) > 0:
        logger.info(
            "Mechanical verdict: REQUEST_CHANGES (%d critical issues)",
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"
    for sc in getattr(review, "scorecard", []):
        sc_status = getattr(sc, "status", "")
        if sc_status == "FAIL":
            logger.info(
                "Mechanical verdict: REQUEST_CHANGES (scorecard dimension failed: %s)", sc.dimension
            )
            return "REQUEST_CHANGES"
        elif sc_status in ("UNKNOWN", "ERROR"):
            logger.warning(
                "Scorecard dimension has unverified status (%s): %s", sc_status, sc.dimension
            )
    if getattr(review, "verdict", None) == "REQUEST_CHANGES":
        logger.debug("Mechanical verdict: REQUEST_CHANGES (explicit review verdict)")
        return "REQUEST_CHANGES"
    if getattr(review, "verdict", None) == "COMMENT":
        return "COMMENT"
    logger.debug("Mechanical verdict: APPROVE (0 critical issues)")
    return "APPROVE"


def calculate_sync_verdict(review: SyncReviewResponse) -> str:
    """Calculate sync re-review verdict mechanically from resolutions and new issues."""
    unresolved_critical = [
        r
        for r in review.resolutions
        if r.status == "UNRESOLVED" and getattr(r, "category", "CRITICAL") == "CRITICAL"
    ]
    if unresolved_critical or len(review.critical_issues) > 0:
        logger.debug(
            "Sync verdict: REQUEST_CHANGES (unresolved_critical=%d, critical=%d)",
            len(unresolved_critical),
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"
    for sc in getattr(review, "scorecard", []):
        sc_status = getattr(sc, "status", "")
        if sc_status == "FAIL":
            logger.debug(
                "Sync verdict: REQUEST_CHANGES (scorecard dimension failed: %s)", sc.dimension
            )
            return "REQUEST_CHANGES"
        elif sc_status in ("UNKNOWN", "ERROR"):
            logger.warning(
                "Sync scorecard dimension has unverified status (%s): %s", sc_status, sc.dimension
            )
    if getattr(review, "verdict", None) == "REQUEST_CHANGES":
        logger.debug("Sync verdict: REQUEST_CHANGES (explicit review verdict)")
        return "REQUEST_CHANGES"
    if getattr(review, "verdict", None) == "COMMENT":
        return "COMMENT"
    logger.debug(
        "Sync verdict: APPROVE (all items RESOLVED, %d minor suggestions)",
        len(review.minor_suggestions),
    )
    return "APPROVE"


def parse_text_review_to_dict(body: str) -> dict[str, Any]:
    """Parse loose Markdown text review body into structured dictionary for CodeReviewResponse."""
    data: dict[str, Any] = {}
    exec_section_match = re.search(
        r"###\s*(?:\d+\.)?\s*(?:Executive Summary|Synchronization Summary)[^\n]*\n+([\s\S]*?)(?=\n---|\n###|\Z)",
        body,
        re.IGNORECASE,
    )
    if exec_section_match:
        raw_text = exec_section_match.group(1).strip()
        cleaned_text = re.sub(
            r"^(?:(?:\*|-|•)\s*)*(?:\*\*)?(?:Summary & Justification|Update Summary|Executive Summary|Goal of the PR):\*\*?\s*",
            "",
            raw_text,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned_text:
            data["executive_summary"] = cleaned_text
    else:
        summary_match = re.search(
            r"(?:Summary & Justification|Goal of the PR):\*\*?\s*([^\n]+)", body, re.IGNORECASE
        )
        if summary_match:
            data["executive_summary"] = summary_match.group(1).strip("* -•` ")
        else:
            for line in body.splitlines():
                sline = line.strip("* -•` ")
                if re.match(
                    r"^(?:\*\*)?(?:Summary & Justification|Executive Summary|Update Summary|Goal of the PR):\*\*?\s*",
                    sline,
                    re.IGNORECASE,
                ):
                    data["executive_summary"] = re.sub(
                        r"^(?:\*\*)?(?:Summary & Justification|Executive Summary|Update Summary|Goal of the PR):\*\*?\s*",
                        "",
                        sline,
                        flags=re.IGNORECASE,
                    ).strip()
                    break

    risks: list[dict[str, str]] = []
    risk_matches = re.findall(
        r"(?:\*?\s*\*\*?Risk:\*\*?|Potential Edge Case / Risk:)\s*([^\n]+)(?:\n\s*\*?\s*(?:\*?\s*\*\*?Recommendation:\*\*?|Recommended Safeguard:)\s*([^\n]+))?",
        body,
        re.IGNORECASE,
    )
    for r_text, s_text in risk_matches:
        clean_r = r_text.strip("* -•")
        if clean_r:
            risks.append(
                {"risk": clean_r, "recommendation": s_text.strip("* -•") if s_text else ""}
            )
    data["risks_and_edge_cases"] = risks

    critical_issues: list[dict[str, Any]] = []
    minor_suggestions: list[dict[str, Any]] = []
    current_section = None
    NON_ISSUE_TOKENS = {
        "5/5",
        "4/5",
        "3/5",
        "2/5",
        "1/5",
        "APPROVE",
        "APPROVED",
        "NONE",
        "NONE FOUND",
        "NONE IDENTIFIED",
        "NONE FOUND.",
        "N/A",
        "PASSED",
        "OK",
        "CLEAN",
        "SUCCESS",
        "NO ISSUES",
        "NO CRITICAL ISSUES",
        "NO CRITICAL ISSUES FOUND",
        "NONE IDENTIFIED FOR THIS PR SCOPE.",
        "NONE IDENTIFIED FOR THIS PR SCOPE",
    }
    NON_ISSUE_PATHS = {
        "CODEBASE",
        "OVERALL",
        "SUMMARY",
        "AUDITOR CONFIDENCE",
        "CONFIDENCE",
        "RATING",
        "SCORE",
        "JUSTIFICATION",
        "SUMMARY & JUSTIFICATION",
    }
    pending_path: str | None = None
    pending_line: int | None = None

    for line in body.splitlines():
        line_s = line.strip()
        if "Critical" in line_s:
            current_section = "critical"
            pending_path = pending_line = None
            continue
        elif "Minor" in line_s or "Refactoring" in line_s or "Suggestion" in line_s:
            current_section = "minor"
            pending_path = pending_line = None
            continue
        elif (
            "Potential Risk" in line_s
            or "Edge Case" in line_s
            or "Executive Summary" in line_s
            or "Section" in line_s
            or line_s.startswith("##")
        ):
            current_section = "other"
            pending_path = pending_line = None
            continue

        if current_section in ("critical", "minor") and line_s.startswith(("*", "-", "•")):
            raw_content = line_s.lstrip("*-• ").strip()
            if not raw_content:
                continue
            norm_content = raw_content.strip("`* :.").upper()
            if (
                norm_content in NON_ISSUE_TOKENS
                or "NONE FOUND" in norm_content
                or "NONE IDENTIFIED" in norm_content
                or norm_content.startswith(("APPROVE", "5/5"))
            ):
                continue
            clean_header = (
                raw_content.replace("🔴", "").replace("🟡", "").replace("✅", "").strip("`* ")
            )
            loc_match = re.match(
                r"^`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_\-]+)`?(?:\s*(?:[\(:]\s*(?:Line\s*)?(\d+)\)?|,\s*Line\s*(\d+)))?\s*$",
                clean_header,
                re.IGNORECASE,
            )
            sep_match = None
            if not loc_match:
                for sep in (":", " - "):
                    if sep in raw_content:
                        p_left, p_right = raw_content.split(sep, 1)
                        clean_left = (
                            p_left.replace("🔴", "")
                            .replace("🟡", "")
                            .replace("✅", "")
                            .strip("`* ")
                        )
                        candidate_loc = re.match(
                            r"^`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_\-]+)`?(?:\s*(?:[\(:]\s*(?:Line\s*)?(\d+)\)?|,\s*Line\s*(\d+)))?\s*$",
                            clean_left,
                            re.IGNORECASE,
                        )
                        if candidate_loc:
                            sep_match = (candidate_loc, p_right.strip())
                            break
            if loc_match:
                pending_path = loc_match.group(1)
                line_str = loc_match.group(2) or loc_match.group(3)
                pending_line = int(line_str) if line_str else None
                continue
            if sep_match:
                candidate_loc, desc_text = sep_match
                path_str = candidate_loc.group(1)
                line_str = candidate_loc.group(2) or candidate_loc.group(3)
                target_path = path_str if ("/" in path_str or "." in path_str) else ""
                target_line = int(line_str) if line_str else None
                clean_desc = desc_text if desc_text else raw_content
                pending_path = pending_line = None
            elif pending_path:
                target_path = pending_path
                target_line = pending_line
                clean_desc = raw_content
                pending_path = pending_line = None
            else:
                parts = raw_content.split(":", 1) if ":" in raw_content else [raw_content]
                raw_path = (
                    parts[0].replace("🔴", "").replace("🟡", "").replace("✅", "").strip("`* ")
                )
                desc_part = parts[1].strip() if len(parts) > 1 else ""
                clean_desc = desc_part if desc_part else raw_content
                raw_path_norm = raw_path.strip().upper()
                clean_desc_norm = clean_desc.strip("`* :.").upper()
                if (
                    not clean_desc
                    or clean_desc.strip("`* :") == raw_path
                    or clean_desc_norm in NON_ISSUE_TOKENS
                    or raw_path_norm in NON_ISSUE_PATHS
                    or "NONE FOUND" in clean_desc_norm
                    or "NONE IDENTIFIED" in clean_desc_norm
                    or clean_desc_norm.startswith(("APPROVE", "5/5"))
                ):
                    continue
                target_path = raw_path if ("/" in raw_path or "." in raw_path) else ""
                target_line = None

            if clean_desc and clean_desc.strip("`* :."):
                item_dict = {
                    "path": target_path,
                    "line": target_line,
                    "description": clean_desc,
                    "suggested_fix": "",
                }
                if current_section == "critical":
                    critical_issues.append(item_dict)
                elif current_section == "minor":
                    minor_suggestions.append(item_dict)

    data["critical_issues"] = critical_issues
    data["minor_suggestions"] = minor_suggestions

    scorecard: list[dict[str, str]] = []
    sc_match = re.search(
        r"###\s*(?:\d+\.)?\s*Audit Dimension Scorecard[^\n]*\n+([\s\S]*?)(?=\n---|\n###|\Z)",
        body,
        re.IGNORECASE,
    )
    if sc_match:
        from .schemas import clean_field_string

        for row in sc_match.group(1).splitlines():
            row_s = row.strip()
            if (
                row_s.startswith("|")
                and not row_s.startswith("| :---")
                and not row_s.lower().startswith("| dimension")
                and not row_s.lower().startswith("| **dimension")
            ):
                cols = [c.strip() for c in row_s.strip("|").split("|")]
                if len(cols) >= 3:
                    dim = clean_field_string(cols[0])
                    st = "UNKNOWN"
                    col1_u = cols[1].upper()
                    if "FAIL" in col1_u or "CRIT" in col1_u or "BLOCK" in col1_u:
                        st = "FAIL"
                    elif "ERR" in col1_u or "CRASH" in col1_u or "EXCEPT" in col1_u:
                        st = "ERROR"
                    elif "UNKNOWN" in col1_u or "UNVERIF" in col1_u:
                        st = "UNKNOWN"
                    elif "WARN" in col1_u or "CAUTION" in col1_u:
                        st = "WARN"
                    elif "N/A" in col1_u or "NA" in col1_u:
                        st = "N/A"
                    elif "PASS" in col1_u or "OK" in col1_u:
                        st = "PASS"
                    notes = clean_field_string(cols[2])
                    if dim:
                        scorecard.append({"dimension": dim, "status": st, "notes": notes})
    if scorecard:
        data["scorecard"] = scorecard

    verified_invariants: list[str] = []
    inv_match = re.search(
        r"###\s*(?:\d+\.)?\s*Verified Invariants[^\n]*\n+([\s\S]*?)(?=\n---|\n###|\Z)",
        body,
        re.IGNORECASE,
    )
    if inv_match:
        for line in inv_match.group(1).splitlines():
            line_s = line.strip()
            if line_s.startswith(("- [x]", "- [ ]", "* ", "- ")):
                inv_clean = re.sub(r"^(?:-\s*\[[ xX]\]|\*|-)\s*", "", line_s).strip()
                if inv_clean:
                    verified_invariants.append(inv_clean)
    if verified_invariants:
        data["verified_invariants"] = verified_invariants

    has_structured_content = any(
        data.get(k)
        for k in (
            "executive_summary",
            "critical_issues",
            "minor_suggestions",
            "risks_and_edge_cases",
            "scorecard",
            "verified_invariants",
        )
    )
    if not has_structured_content:
        return {}

    if "executive_summary" not in data:
        lines = [
            line.strip("* -•` ")
            for line in body.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if lines:
            data["executive_summary"] = lines[0]

    return data


def render_code_review_markdown(review: CodeReviewResponse, verdict: str | None = None) -> str:
    """Render CodeReviewResponse into clean, modern GitHub Markdown."""
    if verdict is None:
        verdict = calculate_strict_verdict(review)
    return review.to_markdown(verdict=verdict)


def render_sync_review_markdown(
    review: SyncReviewResponse,
    verdict: str | None = None,
    has_prior_reviews: bool = True,
) -> str:
    """Render SyncReviewResponse into clean, modern GitHub Markdown."""
    if verdict is None and has_prior_reviews:
        verdict = calculate_sync_verdict(review)
    return review.to_markdown(verdict=verdict, has_prior_reviews=has_prior_reviews)
