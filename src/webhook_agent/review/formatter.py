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

from .comment_poster import defang_images
from .metadata import serialize_review_metadata
from .normalizer import (
    clean_field_string as clean_field_string,
)
from .normalizer import (
    is_implausible_body as is_implausible_body,
)
from .normalizer import (
    is_not_cheap_finding as is_not_cheap_finding,
)
from .schemas import (
    CodeReviewResponse,
    SyncReviewResponse,
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
UNTERMINATED_FENCED_BLOCK = re.compile(r"```(?:json)?\s*([\{\[].*)", re.DOTALL)


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
                "file_path",
                "filename",
                "filepath",
                "description",
                "title",
                "risk",
                "summary",
                "item_description",
                "critical_issues",
                "minor_suggestions",
                "resolutions",
                "candidate_findings",
                "scorecard",
                "dimension",
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
    if not fenced_blocks:
        unterminated_match = UNTERMINATED_FENCED_BLOCK.search(text)
        if unterminated_match:
            candidate_blocks.append(unterminated_match.group(1).strip())
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


STRICTNESS_MAP = {"REQUEST_CHANGES": 3, "COMMENT": 2, "APPROVE": 1}


def clamp_verdict(mechanical_verdict: str, llm_verdict: str | None) -> str:
    """Clamp review verdict: the LLM may only increase strictness, never loosen it."""
    mech_level = STRICTNESS_MAP.get((mechanical_verdict or "").upper(), 1)
    llm_level = STRICTNESS_MAP.get((llm_verdict or "").upper(), 1) if llm_verdict else 1
    final_level = max(mech_level, llm_level)
    for v, lvl in STRICTNESS_MAP.items():
        if lvl == final_level:
            return v
    return mechanical_verdict


def calculate_strict_verdict(review: CodeReviewResponse) -> str:
    """Calculate PR review verdict mechanically from structured issues, scorecard, and invariants."""
    # Hard blocker: any critical issue or failing scorecard dimension -> REQUEST_CHANGES
    if len(review.critical_issues) > 0:
        logger.info(
            "Mechanical verdict: REQUEST_CHANGES (%d critical issues)",
            len(review.critical_issues),
        )
        return "REQUEST_CHANGES"

    for sc in getattr(review, "scorecard", []) or []:
        sc_status = (getattr(sc, "status", "") or "").upper()
        if sc_status == "FAIL":
            logger.info(
                "Mechanical verdict: REQUEST_CHANGES (scorecard dimension failed: %s)",
                sc.dimension,
            )
            return "REQUEST_CHANGES"

    has_unverified_or_warning = False
    for sc in getattr(review, "scorecard", []) or []:
        sc_status = (getattr(sc, "status", "") or "").upper()
        if sc_status in ("WARN", "UNKNOWN", "ERROR"):
            has_unverified_or_warning = True
            logger.info(
                "Scorecard dimension has unverified or warning status (%s): %s",
                sc_status,
                sc.dimension,
            )

    if getattr(review, "verdict", None) == "REQUEST_CHANGES":
        return "REQUEST_CHANGES"
    if getattr(review, "verdict", None) == "COMMENT":
        return "COMMENT"

    # When verdict is None (unspecified), warnings or unverified claims demote to COMMENT
    if getattr(review, "verdict", None) is None and (
        has_unverified_or_warning or bool(getattr(review, "unverified_claims", None))
    ):
        return "COMMENT"

    return "APPROVE"


def calculate_sync_verdict(review: SyncReviewResponse) -> str:
    """Calculate sync re-review verdict mechanically from resolutions, new issues, and scorecard."""
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

    for sc in getattr(review, "scorecard", []) or []:
        sc_status = (getattr(sc, "status", "") or "").upper()
        if sc_status == "FAIL":
            logger.debug(
                "Sync verdict: REQUEST_CHANGES (scorecard dimension failed: %s)", sc.dimension
            )
            return "REQUEST_CHANGES"

    # Any unresolved non-critical resolution (SUGGESTION or RISK) strictly blocks APPROVE!
    unresolved_non_critical = [
        r
        for r in review.resolutions
        if r.status == "UNRESOLVED" and getattr(r, "category", "CRITICAL") != "CRITICAL"
    ]

    has_unverified_or_warning = False
    for sc in getattr(review, "scorecard", []) or []:
        sc_status = (getattr(sc, "status", "") or "").upper()
        if sc_status in ("WARN", "UNKNOWN", "ERROR"):
            has_unverified_or_warning = True
            logger.info(
                "Sync scorecard dimension has unverified or warning status (%s): %s",
                sc_status,
                sc.dimension,
            )

    if getattr(review, "verdict", None) == "REQUEST_CHANGES":
        return "REQUEST_CHANGES"

    # If ANY prior item is unresolved, verdict CANNOT be APPROVE: it MUST be COMMENT!
    if unresolved_non_critical:
        logger.info(
            "Sync verdict: COMMENT (%d unresolved non-critical items cannot approve)",
            len(unresolved_non_critical),
        )
        return "COMMENT"

    if getattr(review, "verdict", None) == "COMMENT":
        return "COMMENT"

    if getattr(review, "verdict", None) is None and (
        has_unverified_or_warning or bool(getattr(review, "unverified_claims", None))
    ):
        return "COMMENT"

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


def render_code_review_markdown(
    review: CodeReviewResponse, verdict: str | None = None, **kwargs: Any
) -> str:
    """Render CodeReviewResponse into clean, modern GitHub Markdown with persistent 8-section layout."""
    if verdict is None:
        verdict = calculate_strict_verdict(review)
    verdict_str = verdict or review.verdict or "COMMENT"
    verdict_badge = f"`{verdict_str}`"

    markdown_parts = [
        f"## 🛡️ Code Review: {verdict_badge}",
        "",
        "### 1. Executive Summary",
        "",
        review.executive_summary,
        "",
        "---",
        "",
        "### 2. Resolution Tracker",
        "",
        "_Initial review round; no prior resolutions._",
    ]

    # Section 3: Audit Dimension Scorecard
    scorecard_lines = ["", "---", "", "### 3. Audit Dimension Scorecard", ""]
    if review.scorecard:
        scorecard_rows = [item.to_table_row() for item in review.scorecard]
        scorecard_lines.extend(
            [
                "| Dimension | Status | Notes |",
                "| :--- | :---: | :--- |",
                *scorecard_rows,
            ]
        )
    else:
        scorecard_lines.append("_None recorded._")
    markdown_parts.extend(scorecard_lines)

    # Section 4: Action Items & Findings
    action_lines = ["", "---", "", "### 4. Action Items & Findings"]
    action_lines.extend(
        [
            "",
            "#### 🔴 Critical (Must Fix Before Merge)",
            "\n".join(issue.to_markdown() for issue in review.critical_issues)
            if review.critical_issues
            else "_None recorded._",
        ]
    )
    action_lines.extend(
        [
            "",
            "#### 🟡 Suggestions & Maintainability",
            "\n".join(sugg.to_markdown() for sugg in review.minor_suggestions)
            if review.minor_suggestions
            else "_None recorded._",
        ]
    )
    markdown_parts.extend(action_lines)

    # Section 5: Potential Risks & Edge Cases
    risk_lines: list[str] = []
    if review.risks_and_edge_cases:
        for item in review.risks_and_edge_cases:
            risk_lines.append(f"* **Risk:** {item.risk}")
            if item.recommendation and item.recommendation.strip():
                risk_lines.append(f"  * *Recommendation*: {item.recommendation.strip()}")
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 5. Potential Risks & Edge Cases",
            "",
            "\n".join(risk_lines) if risk_lines else "_None recorded._",
        ]
    )

    # Section 6: Verified Invariants
    inv_lines = [f"- [x] {inv}" for inv in review.verified_invariants]
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 6. Verified Invariants",
            "",
            "\n".join(inv_lines) if inv_lines else "_None recorded._",
        ]
    )

    # Section 7: Semantic Invariants (Model-Asserted)
    sem_lines = [f"* {inv}" for inv in review.semantic_invariants]
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 7. Semantic Invariants (Model-Asserted)",
            "",
            "\n".join(sem_lines) if sem_lines else "_None recorded._",
        ]
    )

    # Section 8: Unverified Claims & Residual Uncertainty
    gap_lines: list[str] = []
    if review.unverified_claims:
        for claim in review.unverified_claims:
            gap_lines.append(f"* {claim}")
    if review.context_gaps:
        gaps_str = ", ".join(review.context_gaps)
        gap_lines.append(f"* **Context Gaps:** {gaps_str}")
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 8. Residual Uncertainty & Context Gaps",
            "",
            "\n".join(gap_lines) if gap_lines else "_None recorded._",
        ]
    )

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
            for issue in review.critical_issues
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
            for sugg in review.minor_suggestions
        ],
        "risks": [
            {
                "risk": item.risk,
                "recommendation": item.recommendation,
                "category": "RISK",
            }
            for item in review.risks_and_edge_cases
        ],
        "unverified_claims": list(review.unverified_claims),
    }
    if review.scorecard:
        meta["scorecard"] = [
            {"dimension": s.dimension, "status": s.status, "notes": s.notes}
            for s in review.scorecard
        ]
    if review.verified_invariants:
        meta["verified_invariants"] = list(review.verified_invariants)
    if review.semantic_invariants:
        meta["semantic_invariants"] = list(review.semantic_invariants)
    if review.model:
        meta["model"] = review.model
    rendered_body = defang_images("\n".join(markdown_parts))
    metadata_comment = serialize_review_metadata(meta)
    return f"{rendered_body}\n\n{metadata_comment}\n"


def render_sync_review_markdown(
    review: SyncReviewResponse,
    verdict: str | None = None,
    has_prior_reviews: bool = True,
    **kwargs: Any,
) -> str:
    """Render SyncReviewResponse into clean, modern GitHub Markdown."""
    if not has_prior_reviews:
        cr_data = {
            "executive_summary": review.summary or "Autonomous PR code review report.",
            "verdict": review.verdict or verdict,
            "scorecard": [s.model_dump() for s in review.scorecard],
            "critical_issues": [item.model_dump() for item in review.critical_issues],
            "minor_suggestions": [item.model_dump() for item in review.minor_suggestions],
            "risks_and_edge_cases": [],
            "verified_invariants": list(review.verified_invariants),
            "semantic_invariants": list(review.semantic_invariants),
            "unverified_claims": list(review.unverified_claims),
            "model": review.model,
        }
        cr_obj = CodeReviewResponse.model_validate(cr_data)
        return render_code_review_markdown(cr_obj, verdict=verdict)

    if verdict is None:
        verdict = calculate_sync_verdict(review)

    verdict_str = verdict or review.verdict or "COMMENT"
    verdict_badge = f"`{verdict_str}`"

    res_lines: list[str] = []
    if review.resolutions:
        crit_res = [
            r for r in review.resolutions if getattr(r, "category", "CRITICAL") == "CRITICAL"
        ]
        sugg_res = [
            r for r in review.resolutions if getattr(r, "category", "CRITICAL") == "SUGGESTION"
        ]
        risk_res = [r for r in review.resolutions if getattr(r, "category", "CRITICAL") == "RISK"]

        has_categories = any(
            getattr(r, "category", "CRITICAL") in ("SUGGESTION", "RISK") for r in review.resolutions
        )
        if not has_categories:
            for item in review.resolutions:
                cat = getattr(item, "category", "CRITICAL")
                if item.status == "RESOLVED":
                    icon = "✅"
                elif cat == "SUGGESTION":
                    icon = "🟡"
                elif cat == "RISK":
                    icon = "🛡️"
                else:
                    icon = "🔴"
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
    if review.critical_issues:
        for issue in review.critical_issues:
            crit_lines.append(issue.to_markdown(prefix="🔴"))

    minor_lines: list[str] = []
    if review.minor_suggestions:
        for issue in review.minor_suggestions:
            minor_lines.append(issue.to_markdown(prefix="🟡"))

    markdown_parts = [
        f"## ⚡ Code Review Update: {verdict_badge}",
        "",
        "### 1. Synchronization Summary",
        "",
        review.summary,
        "",
        "---",
        "",
        "### 2. Resolution Tracker",
        "",
        "\n".join(res_lines) if res_lines else "_None recorded._",
    ]

    # Section 3: Audit Dimension Scorecard
    scorecard_lines = ["", "---", "", "### 3. Audit Dimension Scorecard", ""]
    if review.scorecard:
        scorecard_rows = [item.to_table_row() for item in review.scorecard]
        scorecard_lines.extend(
            [
                "| Dimension | Status | Notes |",
                "| :--- | :---: | :--- |",
                *scorecard_rows,
            ]
        )
    else:
        scorecard_lines.append("_None recorded._")
    markdown_parts.extend(scorecard_lines)

    # Section 4: New Findings (Introduced in Update)
    finding_lines = ["", "---", "", "### 4. New Findings (Introduced in Update)"]
    finding_lines.extend(
        [
            "",
            "#### 🔴 Critical (Must Fix Before Merge)",
            "\n".join(crit_lines) if crit_lines else "_None recorded._",
        ]
    )
    finding_lines.extend(
        [
            "",
            "#### 🟡 Suggestions & Maintainability",
            "\n".join(minor_lines) if minor_lines else "_None recorded._",
        ]
    )
    markdown_parts.extend(finding_lines)

    # Section 5: Potential Risks & Edge Cases
    risk_lines: list[str] = []
    for r_item in getattr(review, "risks_and_edge_cases", []) or []:
        risk_lines.append(f"* **Risk:** {r_item.risk}")
        if r_item.recommendation and r_item.recommendation.strip():
            risk_lines.append(f"  * *Recommendation*: {r_item.recommendation.strip()}")
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 5. Potential Risks & Edge Cases",
            "",
            "\n".join(risk_lines) if risk_lines else "_None recorded._",
        ]
    )

    # Section 6: Verified Invariants
    inv_lines = [f"- [x] {inv}" for inv in review.verified_invariants]
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 6. Verified Invariants",
            "",
            "\n".join(inv_lines) if inv_lines else "_None recorded._",
        ]
    )

    # Section 7: Semantic Invariants (Model-Asserted)
    sem_lines = [f"* {inv}" for inv in review.semantic_invariants]
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 7. Semantic Invariants (Model-Asserted)",
            "",
            "\n".join(sem_lines) if sem_lines else "_None recorded._",
        ]
    )

    # Section 8: Residual Uncertainty & Context Gaps
    u_lines = [f"* {claim}" for claim in review.unverified_claims]
    markdown_parts.extend(
        [
            "",
            "---",
            "",
            "### 8. Residual Uncertainty & Context Gaps",
            "",
            "\n".join(u_lines) if u_lines else "_None recorded._",
        ]
    )

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
            for item in review.resolutions
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
            for issue in review.critical_issues
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
            for sugg in review.minor_suggestions
        ],
        "risks": [
            {
                "risk": r_item.risk,
                "recommendation": r_item.recommendation,
                "category": "RISK",
            }
            for r_item in getattr(review, "risks_and_edge_cases", []) or []
        ],
        "unverified_claims": list(review.unverified_claims),
    }
    if review.scorecard:
        meta["scorecard"] = [
            {"dimension": s.dimension, "status": s.status, "notes": s.notes}
            for s in review.scorecard
        ]
    if review.verified_invariants:
        meta["verified_invariants"] = list(review.verified_invariants)
    if review.semantic_invariants:
        meta["semantic_invariants"] = list(review.semantic_invariants)
    if review.model:
        meta["model"] = review.model
    rendered_body = defang_images("\n".join(markdown_parts))
    metadata_comment = serialize_review_metadata(meta)
    return f"{rendered_body}\n\n{metadata_comment}\n"
