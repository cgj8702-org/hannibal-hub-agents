"""Canary validation module used for testing live webhook agent capabilities."""

from __future__ import annotations

import logging
from typing import Any

from webhook_agent.models.model_chain import is_gemini_3_plus

logger = logging.getLogger("webhook_agent.analysis.audit_canary")


def calculate_canary_complexity(tokens: list[str]) -> float:
    """Compute average token length for symbol complexity metrics.

    Returns 0.0 if the input token list is empty to prevent ZeroDivisionError.
    """
    if not tokens:
        return 0.0
    total_length = sum(len(t) for t in tokens)
    return total_length / len(tokens)


def assess_model_suitability(model_name: str) -> dict[str, Any]:
    """Inspect model capabilities using centralized chain utilities."""
    is_v3 = is_gemini_3_plus(model_name)
    return {
        "model": model_name,
        "is_gemini_3_plus": is_v3,
        "supports_explicit_caching": is_v3,
        "recommended_for_heavy_pr": is_v3,
    }


def format_canary_summary(report_data: dict[str, Any]) -> str:
    """Format canary assessment findings into a concise markdown section."""
    lines = ["### Canary Auditor Assessment", ""]
    for key, value in report_data.items():
        lines.append(f"- **{key}**: `{value}`")
    return "\n".join(lines)
