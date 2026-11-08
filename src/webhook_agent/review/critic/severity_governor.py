"""Severity Governor for review findings and comment prefixing.

Adapted from google/adk-samples/.github/scripts/post_review_comments.py.

Rules:
1. LLM findings cannot unilaterally declare themselves CRITICAL.
   A model asked to grade its own severity inflates, and a confident, false
   "this is critical" is the most expensive comment this system can produce.
2. CRITICAL is reserved strictly for deterministic compiler / scanner findings:
   - AST syntax / compiler errors
   - Proven security scanner violations (PROVEN confidence, CRITICAL/HIGH severity)
   - Proven broken contract / symbol call sites (PROVEN confidence)
   - Hard CI failures
3. Model-identified defects are governed into:
   - MAJOR: Logic bugs, boundaries, concurrency, crash paths, unhandled exceptions.
   - MINOR: Maintainability, documentation drift, hygiene, structural suggestions.
4. Voicing / Prefixing:
   - Severity tags [CRITICAL], [MAJOR], [MINOR] are prepended mechanically to comment bodies.
   - Inline comments are ordered strictly: CRITICAL first, then MAJOR, then MINOR.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from webhook_agent.review.schemas import CandidateFinding, IssueItem

logger = logging.getLogger("webhook_agent.review.critic.severity_governor")

TAG_CRITICAL = "[CRITICAL]"
TAG_MAJOR = "[MAJOR]"
TAG_MINOR = "[MINOR]"

TAG_PREFIX = re.compile(r"^\s*\[(?:CRITICAL|MAJOR|MINOR)\]\s*", re.IGNORECASE)

# Mapping of defect categories/lanes to standard severity tags
CATEGORY_TAG_MAP = {
    # Critical compiler/deterministic categories
    "SYNTAX": TAG_CRITICAL,
    "AST": TAG_CRITICAL,
    "CI_FAIL": TAG_CRITICAL,
    "PROVEN_SECURITY": TAG_CRITICAL,
    "PROVEN_CONTRACT": TAG_CRITICAL,
    "PROVEN_LOOP": TAG_CRITICAL,
    "PROVEN_ARCHITECTURE": TAG_CRITICAL,
    "PROVEN_BOUNDARY": TAG_CRITICAL,
    "LOCKFILE": TAG_CRITICAL,
    "TAUTOLOGY": TAG_CRITICAL,
    # Major logic/correctness/security categories
    "LOGIC": TAG_MAJOR,
    "BOUNDARIES": TAG_MAJOR,
    "CONCURRENCY": TAG_MAJOR,
    "CORRECTNESS": TAG_MAJOR,
    "SECURITY": TAG_MAJOR,
    "CRASH": TAG_MAJOR,
    "EXCEPTION": TAG_MAJOR,
    # Minor maintainability/hygiene categories
    "MAINTAINABILITY": TAG_MINOR,
    "HYGIENE": TAG_MINOR,
    "DOCS": TAG_MINOR,
    "DOCUMENTATION": TAG_MINOR,
    "STYLE": TAG_MINOR,
    "HOUSE_RULES": TAG_MINOR,
}

SEVERITY_ORDER = {
    TAG_CRITICAL: 0,
    TAG_MAJOR: 1,
    TAG_MINOR: 2,
}


def strip_severity_tag(body: str) -> str:
    """Remove any leading [CRITICAL], [MAJOR], or [MINOR] tag from body."""
    return TAG_PREFIX.sub("", str(body or "")).lstrip()


def apply_severity_tag(body: str, tag: str) -> str:
    """Prefix body with tag without stacking multiple tags."""
    clean = strip_severity_tag(body)
    return f"{tag} {clean}" if clean else tag


class SeverityGovernor:
    """Governs finding severity levels against deterministic compiler signals."""

    def __init__(
        self,
        deterministic_failing_paths: set[str] | None = None,
        deterministic_failing_locations: set[tuple[str, int]] | None = None,
    ) -> None:
        self.failing_paths = {p.strip().lower() for p in (deterministic_failing_paths or set())}
        self.failing_locations = {
            (loc[0].strip().lower(), loc[1]) for loc in (deterministic_failing_locations or set())
        }

    def govern_severity(
        self,
        finding: CandidateFinding | IssueItem | dict[str, Any],
        is_deterministic: bool = False,
    ) -> str:
        """Determine governed severity tag for a finding.

        If is_deterministic is True or the finding location matches a known deterministic
        compiler/scanner failure, CRITICAL is permitted. Otherwise, CRITICAL is downgraded
        to MAJOR.
        """
        raw_sev = ""
        path = ""
        line = 0
        cat = ""

        if isinstance(finding, (CandidateFinding, IssueItem)):
            raw_sev = str(getattr(finding, "severity", "") or "").strip().upper()
            path = (
                str(getattr(finding, "path", getattr(finding, "file_path", "")) or "")
                .strip()
                .lower()
            )
            line = int(getattr(finding, "line", getattr(finding, "line_number", 0)) or 0)
            cat = str(getattr(finding, "category", "") or "").strip().upper()
        elif isinstance(finding, dict):
            raw_sev = str(finding.get("severity", "") or "").strip().upper()
            path = str(finding.get("path") or finding.get("file_path") or "").strip().lower()
            try:
                line = int(finding.get("line") or finding.get("line_number") or 0)
            except (ValueError, TypeError):
                line = 0
            cat = str(finding.get("category", "") or "").strip().upper()

        # Check deterministic match
        has_det_match = is_deterministic or (
            (path, line) in self.failing_locations if line > 0 else path in self.failing_paths
        )

        # Determine candidate target severity tag
        target_tag = TAG_MINOR
        if raw_sev == "CRITICAL":
            target_tag = TAG_CRITICAL
        elif raw_sev in ("HIGH", "MAJOR"):
            target_tag = TAG_MAJOR
        elif raw_sev in ("MEDIUM", "LOW", "MINOR"):
            if cat in ("SECURITY", "LOGIC", "CONCURRENCY", "CORRECTNESS"):
                target_tag = TAG_MAJOR
            elif cat:
                target_tag = CATEGORY_TAG_MAP.get(cat, TAG_MINOR)
            else:
                target_tag = TAG_MINOR
        elif cat:
            target_tag = CATEGORY_TAG_MAP.get(cat, TAG_MINOR)
        else:
            target_tag = TAG_MINOR

        # Mechanical rule: CRITICAL strictly requires a verified deterministic match
        if target_tag == TAG_CRITICAL:
            if has_det_match:
                return TAG_CRITICAL
            logger.info(
                "SeverityGovernor: governed unproven CRITICAL finding at %s:%d (cat=%s) to MAJOR",
                path,
                line,
                cat,
            )
            return TAG_MAJOR

        return target_tag

    def apply_governance_to_issues(
        self,
        issues: Sequence[IssueItem],
        is_deterministic: bool = False,
    ) -> list[IssueItem]:
        """Apply severity governance to a list of IssueItems."""
        governed: list[IssueItem] = []
        for issue in issues:
            tag = self.govern_severity(issue, is_deterministic=is_deterministic)
            sev_name = tag.replace("[", "").replace("]", "")
            # Update severity on the issue object
            issue.severity = sev_name
            governed.append(issue)
        return governed

    def tag_and_sort_comments(
        self,
        comments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ensure comments have severity tags and sort CRITICAL first, then MAJOR, then MINOR.

        Strips any pre-existing model severity tag from comment bodies so that the model
        cannot self-assign or inflate severity, and enforces mechanical governance.
        """
        tagged: list[dict[str, Any]] = []
        for c in comments:
            body = c.get("body", "")
            clean_body = strip_severity_tag(body)

            # Build finding candidate for severity governor inspection
            c_finding = dict(c)
            existing_tag_match = TAG_PREFIX.match(body)
            if existing_tag_match and not c_finding.get("severity"):
                matched_str = existing_tag_match.group(0).strip(" []").upper()
                c_finding["severity"] = matched_str

            tag = self.govern_severity(c_finding)
            tagged_body = apply_severity_tag(clean_body, tag)

            updated = dict(c)
            updated["body"] = tagged_body
            updated["_sort_rank"] = SEVERITY_ORDER.get(tag, 3)
            tagged.append(updated)

        # Sort: first by severity rank (CRITICAL=0, MAJOR=1, MINOR=2), then path, then line
        tagged.sort(
            key=lambda x: (x.get("_sort_rank", 3), str(x.get("path", "")), int(x.get("line", 0)))
        )
        for item in tagged:
            item.pop("_sort_rank", None)
            item.pop("severity", None)
            item.pop("category", None)
        return tagged
