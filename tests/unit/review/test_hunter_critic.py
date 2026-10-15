"""Unit tests for Hunter and Critic competence review engines."""

from __future__ import annotations

import pytest

from webhook_agent.review.critic import CriticEngine
from webhook_agent.review.hunter import HunterEngine
from webhook_agent.review.schemas import CandidateFinding, IssueItem, VerificationVerdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_hunter_parse_candidate_findings() -> None:
    hunter = HunterEngine()
    raw = [
        {
            "id": "cand-1",
            "file_path": "src/app.py",
            "line_number": 10,
            "severity": "high",
            "confidence_score": 9,
            "title": "Unclosed resource",
            "description": "Session is not closed.",
            "suggested_fix": "session.close()",
            "falsification_check": "Verify if session has auto-cleanup context.",
        },
        {
            # Test fallback keys and default confidence
            "path": "src/utils.py",
            "line": 25,
            "severity": "nit",
            "description": "Variable naming could be clearer.",
        },
        # Already a CandidateFinding
        CandidateFinding(
            id="cand-3",
            file_path="src/main.py",
            line_number=1,
            title="Syntax error",
            description="Missing colon",
        ),
        # Non-dict / invalid item
        "invalid_item_string",
    ]

    parsed = hunter.parse_candidate_findings(raw)
    assert len(parsed) == 3

    assert parsed[0].id == "cand-1"
    assert parsed[0].file_path == "src/app.py"
    assert parsed[0].severity == "HIGH"
    assert parsed[0].confidence_score == 9
    assert parsed[0].suggested_fix == "session.close()"

    assert parsed[1].id == "candidate-2"
    assert parsed[1].file_path == "src/utils.py"
    assert parsed[1].line_number == 25
    assert parsed[1].severity == "NIT"
    assert parsed[1].confidence_score == 5

    assert parsed[2].id == "cand-3"
    assert parsed[2].file_path == "src/main.py"


def test_critic_evaluate_candidate_drops_nit() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="NIT",
        confidence_score=9,
        title="Style nit",
        description="Extra blank line",
    )
    assert critic.evaluate_candidate(cand) is False


def test_critic_evaluate_candidate_drops_low_confidence() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="MEDIUM",
        confidence_score=7,
        title="Possible race condition",
        description="Check lock acquisition",
    )
    assert critic.evaluate_candidate(cand) is False


def test_critic_evaluate_candidate_accepts_high_confidence() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="CRITICAL",
        confidence_score=8,
        title="SQL injection",
        description="Unsanitized query string",
    )
    assert critic.evaluate_candidate(cand) is True


def test_critic_evaluate_candidate_with_verdict() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        id="c-1",
        file_path="foo.py",
        severity="HIGH",
        confidence_score=9,
        title="Uncaught error",
        description="Might throw KeyError",
    )

    # Disproven verdict
    disproven = VerificationVerdict(
        finding_id="c-1",
        verified=False,
        confidence_score=2,
        disproof_reason="Outer dict has default key handler.",
    )
    assert critic.evaluate_candidate(cand, verdict=disproven) is False

    # Verified but low confidence verdict
    low_conf = VerificationVerdict(
        finding_id="c-1",
        verified=True,
        confidence_score=7,
    )
    assert critic.evaluate_candidate(cand, verdict=low_conf) is False

    # Verified high confidence verdict
    verified = VerificationVerdict(
        finding_id="c-1",
        verified=True,
        confidence_score=9,
    )
    assert critic.evaluate_candidate(cand, verdict=verified) is True


def test_critic_filter_and_verify() -> None:
    critic = CriticEngine(min_confidence=8)
    candidates = [
        CandidateFinding(
            id="c-1",
            file_path="src/a.py",
            line_number=10,
            severity="HIGH",
            confidence_score=9,
            title="Leaked connection",
            description="Old description",
            suggested_fix="conn.close()",
        ),
        CandidateFinding(
            id="c-2",
            file_path="src/b.py",
            line_number=20,
            severity="NIT",
            confidence_score=10,
            title="Formatting",
            description="Spacing",
        ),
        CandidateFinding(
            id="c-3",
            file_path="src/c.py",
            line_number=30,
            severity="MEDIUM",
            confidence_score=5,
            title="Speculative bug",
            description="Maybe null",
        ),
    ]

    verdicts = {
        "c-1": VerificationVerdict(
            finding_id="c-1",
            verified=True,
            confidence_score=9,
            revised_description="Refined description: connection leak on timeout",
            revised_suggested_fix="try:\n    yield\nfinally:\n    conn.close()",
        )
    }

    survivors = critic.filter_and_verify(candidates, verdicts=verdicts)
    assert len(survivors) == 1
    issue = survivors[0]
    assert isinstance(issue, IssueItem)
    assert issue.path == "src/a.py"
    assert issue.line == 10
    assert "Refined description: connection leak on timeout" in issue.description
    assert issue.suggested_fix == "try:\n    yield\nfinally:\n    conn.close()"
