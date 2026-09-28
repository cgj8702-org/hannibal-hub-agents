"""Unit tests for PR review verdict calculator."""

import pytest

from webhook_agent.webhook_agent import calculate_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_has_critical_triggers_request_changes() -> None:
    assert calculate_verdict(has_critical=True) == "REQUEST_CHANGES"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_clean_approves() -> None:
    assert calculate_verdict(has_critical=False) == "APPROVE"
