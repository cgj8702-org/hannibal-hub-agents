"""Review metadata serialization, extraction, and legacy markdown parsing.

Provides structured machine-readable metadata embedding in GitHub review comments
via HTML comments, and decouples human-readable Markdown rendering from agent prompting.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

METADATA_COMMENT_PATTERN = re.compile(
    r"<!--\s*hannibal-review-metadata:\s*(\{.*?\})\s*-->", re.DOTALL
)


def serialize_review_metadata(metadata: dict[str, Any]) -> str:
    """Serialize structured review metadata into an invisible HTML comment string.

    Uses compact JSON representation (separators=(',', ':')) to ensure clean
    single-line or tight multi-line comment rendering that is hidden on GitHub.
    """
    compact_json = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)
    return f"<!-- hannibal-review-metadata: {compact_json} -->"


def extract_review_metadata(body: str) -> dict[str, Any] | None:
    """Extract and parse structured review metadata from an invisible HTML comment in a review body.

    Returns the parsed dictionary if found and valid JSON, or None if no valid
    metadata comment is present.
    """
    if not body:
        return None

    match = METADATA_COMMENT_PATTERN.search(body)
    if not match:
        return None

    json_str = match.group(1).strip()
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.debug("Failed to deserialize review metadata comment: %s", exc)

    return None


def parse_legacy_review_findings(body: str) -> dict[str, list[dict[str, Any]]]:
    """Fallback parser that extracts findings from legacy human-readable Markdown reviews.

    Correctly isolates sections and checks for actual finding bullet points rather
    than performing naive substring checks that break when one section is empty.
    """
    results: dict[str, list[dict[str, Any]]] = {
        "critical_issues": [],
        "minor_suggestions": [],
        "risks": [],
        "resolutions": [],
    }
    if not body:
        return results

    # Normalize newlines
    normalized = body.replace("\r\n", "\n")

    # Split into sections based on headers
    # Section markers:
    # Critical: #### 🔴 Critical or #### Critical
    # Suggestions: #### 🟡 Suggestions or #### Suggestions
    # Risks: ### 3. Potential Risks or ### Potential Risks
    # Resolutions: ### 2. Resolution Tracker or ### Resolution Tracker

    # Extract Critical section
    crit_match = re.search(
        r"####\s*(?:🔴)?\s*Critical[^\n]*\n([\s\S]*?)(?=\n####|\n###|\n---|\Z)",
        normalized,
        re.IGNORECASE,
    )
    if crit_match:
        section_text = crit_match.group(1).strip()
        if not re.search(r"^\s*\*\s*\*None found\.\*", section_text, re.MULTILINE):
            for line in section_text.split("\n"):
                if line.startswith(("* ", "- ")) and not line.strip().startswith(
                    ("* *None found", "* None found")
                ):
                    clean_desc = re.sub(r"^(?:\*|-)\s*(?:🔴\s*)?(?:\*\*|\[)?", "", line)
                    clean_desc = clean_desc.rstrip(" *]").strip()
                    if clean_desc:
                        results["critical_issues"].append(
                            {
                                "category": "CRITICAL",
                                "description": clean_desc,
                            }
                        )

    # Extract Suggestions section
    sugg_match = re.search(
        r"####\s*(?:🟡)?\s*Suggestions[^\n]*\n([\s\S]*?)(?=\n####|\n###|\n---|\Z)",
        normalized,
        re.IGNORECASE,
    )
    if sugg_match:
        section_text = sugg_match.group(1).strip()
        if not re.search(r"^\s*\*\s*\*None found\.\*", section_text, re.MULTILINE):
            for line in section_text.split("\n"):
                if line.startswith(("* ", "- ")) and not line.strip().startswith(
                    ("* *None found", "* None found")
                ):
                    clean_desc = re.sub(r"^(?:\*|-)\s*(?:🟡\s*)?(?:\*\*|\[)?", "", line)
                    clean_desc = clean_desc.rstrip(" *]").strip()
                    if clean_desc:
                        results["minor_suggestions"].append(
                            {
                                "category": "SUGGESTION",
                                "description": clean_desc,
                            }
                        )

    # Extract Risks section
    risk_match = re.search(
        r"###\s*(?:\d+\.)?\s*Potential Risks[^\n]*\n([\s\S]*?)(?=\n###|\n---|\Z)",
        normalized,
        re.IGNORECASE,
    )
    if risk_match:
        section_text = risk_match.group(1).strip()
        if not re.search(r"^\s*\*\s*\*None identified", section_text, re.MULTILINE):
            for line in section_text.split("\n"):
                if line.startswith(("* ", "- ")) and not line.strip().startswith(
                    ("* *None identified", "* None identified")
                ):
                    clean_risk = re.sub(r"^(?:\*|-)\s*(?:\*\*Risk:\*\*\s*)?", "", line)
                    clean_risk = clean_risk.strip()
                    if clean_risk:
                        results["risks"].append(
                            {
                                "category": "RISK",
                                "description": clean_risk,
                            }
                        )

    # Extract Resolution Tracker section
    res_match = re.search(
        r"###\s*(?:\d+\.)?\s*Resolution Tracker[^\n]*\n([\s\S]*?)(?=\n###|\n---|\Z)",
        normalized,
        re.IGNORECASE,
    )
    if res_match:
        section_text = res_match.group(1).strip()
        if not re.search(r"^\s*\*\s*\*No prior review items", section_text, re.MULTILINE):
            for line in section_text.split("\n"):
                line_s = line.strip()
                if line_s.startswith("* ") and "No prior review items" not in line_s:
                    results["resolutions"].append(
                        {
                            "description": line_s.lstrip("* ").strip(),
                        }
                    )

    return results


def get_actionable_findings(body: str) -> list[dict[str, Any]]:
    """Retrieve all actionable findings (critical, suggestions, risks) from a review body.

    Prefers structured metadata comment if available; falls back to section-aware
    legacy Markdown parsing.
    """
    metadata = extract_review_metadata(body)
    findings: list[dict[str, Any]] = []

    if metadata:
        for crit in metadata.get("critical_issues", []):
            if isinstance(crit, dict):
                findings.append(
                    {
                        "category": "CRITICAL",
                        "description": crit.get("description", ""),
                        "file_path": crit.get("path") or crit.get("file_path"),
                        "line_number": crit.get("line") or crit.get("line_number"),
                        "start_line": crit.get("start_line"),
                        "suggested_fix": crit.get("suggested_fix"),
                    }
                )
        for sugg in metadata.get("minor_suggestions", []):
            if isinstance(sugg, dict):
                findings.append(
                    {
                        "category": "SUGGESTION",
                        "description": sugg.get("description", ""),
                        "file_path": sugg.get("path") or sugg.get("file_path"),
                        "line_number": sugg.get("line") or sugg.get("line_number"),
                        "start_line": sugg.get("start_line"),
                        "suggested_fix": sugg.get("suggested_fix"),
                    }
                )
        for risk in metadata.get("risks", []):
            if isinstance(risk, dict):
                desc = risk.get("risk") or risk.get("description", "")
                findings.append(
                    {
                        "category": "RISK",
                        "description": desc,
                        "recommendation": risk.get("recommendation"),
                    }
                )
        return findings

    # Fallback to legacy parser
    legacy = parse_legacy_review_findings(body)
    findings.extend(legacy["critical_issues"])
    findings.extend(legacy["minor_suggestions"])
    findings.extend(legacy["risks"])
    return findings


def has_actionable_findings(body: str, state: str = "") -> bool:
    """Determine whether a review contains actionable findings or requested changes.

    Robust against cases where one section had 'None found' while another had real issues.
    """
    if (state or "").upper() == "CHANGES_REQUESTED":
        return True

    # Check metadata first
    metadata = extract_review_metadata(body)
    if metadata:
        verdict = str(metadata.get("verdict", "")).upper()
        if verdict == "REQUEST_CHANGES":
            return True
        crit = metadata.get("critical_issues", [])
        sugg = metadata.get("minor_suggestions", [])
        risks = metadata.get("risks", [])
        return bool(crit or sugg or risks)

    # Check review header for explicit verdict
    if re.search(
        r"##\s*(?:🛡️|⚡)?\s*Code Review(?:\s*Update)?:\s*`?REQUEST_CHANGES`?",
        body,
        re.IGNORECASE,
    ):
        return True

    findings = get_actionable_findings(body)
    return len(findings) > 0


def format_findings_for_agent(findings: list[dict[str, Any]]) -> str:
    """Format structured prior review findings into a clean, decoupled text block for agent prompts.

    Eliminates all Markdown formatting clutter (shields, badges, HTML comments),
    giving the LLM a clean, clinical checklist to evaluate against new commits.
    """
    if not findings:
        return "No prior actionable review items tracked (clean pass or approved)."

    lines: list[str] = [f"Tracked prior review items to verify ({len(findings)} total):"]
    for idx, item in enumerate(findings, 1):
        cat = item.get("category", "CRITICAL")
        file_path = item.get("file_path")
        line_num = item.get("line_number")
        desc = item.get("description", "")

        loc_str = ""
        if file_path:
            loc_str = f" [File: {file_path}"
            if line_num:
                loc_str += f":{line_num}"
            loc_str += "]"

        lines.append(f"{idx}. [{cat}]{loc_str} {desc}")
        if item.get("suggested_fix"):
            fix = item["suggested_fix"].strip()
            # Indent the fix
            lines.append(f"   Suggested Fix: {fix}")
        if item.get("recommendation"):
            rec = item["recommendation"].strip()
            lines.append(f"   Recommendation: {rec}")

    return "\n".join(lines)
