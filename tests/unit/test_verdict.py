"""Unit tests for PR review verdict calculator."""

from __future__ import annotations

import pytest

from webhook_agent.review.verdict_parser import calculate_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_has_critical_triggers_request_changes() -> None:
    assert calculate_verdict(has_critical=True) == "REQUEST_CHANGES"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_clean_approves() -> None:
    assert calculate_verdict(has_critical=False) == "APPROVE"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_calculate_verdict_reexports_parity() -> None:
    """Verify re-exports in core and review."""
    from webhook_agent.core.agent_definition import calculate_verdict as core_calc
    from webhook_agent.review import calculate_verdict as review_calc

    assert core_calc is calculate_verdict
    assert review_calc is calculate_verdict
