"""Deterministic 3+ Defect Grouping Engine for Hunter candidate findings.

Lifting patterns from google/adk-samples/.agents/skills/github-pr-review:
Rule: The same defect class in 3 or more places is ONE finding, on the clearest instance,
with the count in evidence/description. Five 'unused import' findings becomes one comment;
the other four slots buy distinct defects. Two instances stay two.
"""

from __future__ import annotations

import re
from collections import defaultdict

from webhook_agent.review.schemas import CandidateFinding

DEFECT_CATEGORIES = (
    ("unused_import", re.compile(r"\b(?:unused|unreferenced)\s+import\b", re.IGNORECASE)),
    (
        "missing_type_annotation",
        re.compile(r"\b(?:missing|untyped)\s+(?:type\s+)?annotation\b", re.IGNORECASE),
    ),
    (
        "unhandled_none",
        re.compile(
            r"\b(?:unhandled|unchecked|missing)\s+(?:none|null)\b|\bnone\s+dereference\b",
            re.IGNORECASE,
        ),
    ),
    ("bare_except", re.compile(r"\bbare\s+except\b|\bexcept\s*:\b", re.IGNORECASE)),
    (
        "unclosed_resource",
        re.compile(
            r"\b(?:unclosed|leaked)\s+(?:file|session|connection|resource|handle)\b", re.IGNORECASE
        ),
    ),
    ("magic_number", re.compile(r"\bmagic\s+(?:number|constant|literal)\b", re.IGNORECASE)),
    ("docstring_drift", re.compile(r"\b(?:stale|missing|outdated)\s+docstring\b", re.IGNORECASE)),
    (
        "naming_convention",
        re.compile(r"\b(?:naming\s+convention|variable\s+naming|rename)\b", re.IGNORECASE),
    ),
    (
        "unnecessary_comprehension",
        re.compile(r"\bunnecessary\s+(?:list\s+)?comprehension\b", re.IGNORECASE),
    ),
)


class DefectGrouper:
    """Collapses 3 or more candidate findings in the same defect category into one."""

    def __init__(self, min_group_count: int = 3) -> None:
        self.min_group_count = min_group_count

    def classify_defect(self, candidate: CandidateFinding) -> str | None:
        """Classify a candidate into a standardized defect category, or None."""
        text = f"{candidate.title} {candidate.description}".lower()
        for cat_id, pattern in DEFECT_CATEGORIES:
            if pattern.search(text):
                return cat_id
        return None

    def group_candidates(self, candidates: list[CandidateFinding]) -> list[CandidateFinding]:
        """Apply 3+ grouping rule to collapse repeated defect instances.

        Returns:
            Collapsed candidate findings list.
        """
        if len(candidates) < self.min_group_count:
            return candidates

        categorized: dict[str, list[CandidateFinding]] = defaultdict(list)
        uncategorized: list[CandidateFinding] = []

        for cand in candidates:
            cat = self.classify_defect(cand)
            if cat:
                categorized[cat].append(cand)
            else:
                uncategorized.append(cand)

        result: list[CandidateFinding] = list(uncategorized)

        for _cat, items in categorized.items():
            if len(items) >= self.min_group_count:
                # Pick the clearest instance: highest confidence score, then lowest line number
                clearest = max(
                    items,
                    key=lambda c: (c.confidence_score, -c.line_number),
                )
                count = len(items)
                other_locs = [
                    f"{c.file_path}:{c.line_number}" for c in items if c.id != clearest.id
                ]
                locs_str = ", ".join(other_locs[:3])
                if len(other_locs) > 3:
                    locs_str += f" and {len(other_locs) - 3} more"

                grouped_title = f"{clearest.title} ({count} instances observed across PR)"
                grouped_desc = (
                    f"{clearest.description}\n\n"
                    f"*(Observed across {count} locations, including {locs_str}; consolidated at 3+ rule.)*"
                )

                collapsed = CandidateFinding(
                    id=clearest.id,
                    file_path=clearest.file_path,
                    line_number=clearest.line_number,
                    start_line=clearest.start_line,
                    severity=clearest.severity,
                    confidence_score=clearest.confidence_score,
                    title=grouped_title,
                    description=grouped_desc,
                    suggested_fix=clearest.suggested_fix,
                    falsification_check=clearest.falsification_check,
                    window=clearest.window,
                    verify_steps=clearest.verify_steps,
                )
                result.append(collapsed)
            else:
                # If fewer than min_group_count (e.g. 2 instances), keep them distinct
                result.extend(items)

        # Preserve original relative order of the findings list
        id_order = {c.id: idx for idx, c in enumerate(candidates)}
        return sorted(result, key=lambda c: id_order.get(c.id, 999999))
