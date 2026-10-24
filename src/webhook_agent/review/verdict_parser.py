"""Review verdict calculation and decision parsing engine."""

from __future__ import annotations

from typing import Any


def calculate_verdict(
    scores: dict[str, int] | None = None,
    has_critical: bool = False,
    **kwargs: Any,
) -> str:
    """Calculates PR review verdict cleanly.

    Rules:
    - If has_critical: REQUEST_CHANGES
    - Otherwise: APPROVE
    """
    if has_critical:
        return "REQUEST_CHANGES"
    return "APPROVE"


__all__ = [
    "calculate_verdict",
]
