"""Unit tests for audit canary validation module."""

from __future__ import annotations

import pytest

from webhook_agent.analysis.audit_canary import (
    assess_model_suitability,
    calculate_canary_complexity,
    format_canary_summary,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
def test_calculate_canary_complexity_standard() -> None:
    """Verify complexity calculation on standard non-empty token lists."""
    tokens = ["alpha", "beta", "gamma"]  # lengths: 5, 4, 5 -> sum = 14 / 3 = 4.666...
    score = calculate_canary_complexity(tokens)
    assert round(score, 2) == 4.67


@pytest.mark.unit
def test_assess_model_suitability() -> None:
    """Verify model suitability assessment classifies Gemini 3+ models properly."""
    gemini_3 = assess_model_suitability("gemini-3.5-flash-lite")
    assert gemini_3["is_gemini_3_plus"] is True
    assert gemini_3["supports_explicit_caching"] is True

    gemini_2 = assess_model_suitability("gemini-2.5-flash")
    assert gemini_2["is_gemini_3_plus"] is False
    assert gemini_2["supports_explicit_caching"] is False


@pytest.mark.unit
def test_format_canary_summary() -> None:
    """Verify markdown formatting of summary report."""
    summary = format_canary_summary({"metric_a": 100, "metric_b": True})
    assert "### Canary Auditor Assessment" in summary
    assert "- **metric_a**: `100`" in summary
    assert "- **metric_b**: `True`" in summary
