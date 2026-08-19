"""Unit tests for mathematical PR review verdict calculator."""

import pytest
from webhook_agent.webhook_agent import calculate_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_low_score_triggers_request_changes() -> None:
    scores = {"correctness": 2, "readability": 5, "architecture": 5}
    assert calculate_verdict(scores, confidence=5) == "REQUEST_CHANGES"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_low_average_triggers_request_changes() -> None:
    scores = {"correctness": 3, "readability": 3, "architecture": 3}
    assert calculate_verdict(scores, confidence=5) == "REQUEST_CHANGES"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_low_confidence_triggers_comment() -> None:
    scores = {"correctness": 5, "readability": 5, "architecture": 5}
    assert calculate_verdict(scores, confidence=3) == "COMMENT"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_high_scores_and_confidence_approves() -> None:
    scores = {"correctness": 5, "readability": 4, "architecture": 4}
    assert calculate_verdict(scores, confidence=5) == "APPROVE"
