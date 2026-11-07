"""Unit tests for DefectGrouper 3+ grouping rule."""

from __future__ import annotations

import pytest

from webhook_agent.review.hunter.grouping import DefectGrouper
from webhook_agent.review.schemas import CandidateFinding

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_defect_grouper_keeps_under_three_instances_distinct() -> None:
    grouper = DefectGrouper()
    candidates = [
        CandidateFinding(
            id="c1",
            file_path="src/a.py",
            line_number=10,
            title="Unused import",
            description="Unused import `sys`",
        ),
        CandidateFinding(
            id="c2",
            file_path="src/b.py",
            line_number=20,
            title="Unused import",
            description="Unused import `os`",
        ),
    ]

    result = grouper.group_candidates(candidates)
    assert len(result) == 2
    assert result[0].id == "c1"
    assert result[1].id == "c2"


def test_defect_grouper_collapses_three_or_more_instances() -> None:
    grouper = DefectGrouper()
    candidates = [
        CandidateFinding(
            id="c1",
            file_path="src/a.py",
            line_number=10,
            confidence_score=7,
            title="Unused import",
            description="Unused import `sys` in a.py",
        ),
        CandidateFinding(
            id="c2",
            file_path="src/b.py",
            line_number=20,
            confidence_score=9,  # Clearest instance
            title="Unused import",
            description="Unused import `os` in b.py",
        ),
        CandidateFinding(
            id="c3",
            file_path="src/c.py",
            line_number=30,
            confidence_score=6,
            title="Unused import",
            description="Unused import `json` in c.py",
        ),
        CandidateFinding(
            id="c4",
            file_path="src/d.py",
            line_number=40,
            confidence_score=5,
            title="Unused import",
            description="Unused import `math` in d.py",
        ),
        CandidateFinding(
            id="c-other",
            file_path="src/e.py",
            line_number=50,
            confidence_score=9,
            title="Unhandled None dereference",
            description="User could be None",
        ),
    ]

    result = grouper.group_candidates(candidates)
    # The 4 unused imports are collapsed into 1; the unhandled None remains separate (total 2)
    assert len(result) == 2

    unused_grouped = next(c for c in result if "Unused import" in c.title)
    assert unused_grouped.id == "c2"  # Chose c2 (confidence 9)
    assert unused_grouped.file_path == "src/b.py"
    assert "4 instances observed across PR" in unused_grouped.title
    assert "Observed across 4 locations" in unused_grouped.description
    assert "src/a.py:10" in unused_grouped.description
    assert "src/c.py:30" in unused_grouped.description


def test_hunter_engine_applies_grouping_during_parsing() -> None:
    from webhook_agent.review.hunter import HunterEngine

    hunter = HunterEngine()
    raw = [
        {
            "id": f"c{i}",
            "file_path": f"src/{i}.py",
            "line_number": i * 10,
            "title": "Missing type annotation",
            "description": "Needs type annotation",
        }
        for i in range(1, 5)
    ]

    parsed = hunter.parse_candidate_findings(raw)
    assert len(parsed) == 1
    assert "4 instances observed across PR" in parsed[0].title
