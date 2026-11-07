"""Cheapness verification for Critic falsification.

Adapted from google/adk-samples/.github/scripts/post_review_comments.py.
Flags findings whose verification steps or falsification checks admit needing
multi-file tracing, speculative attacker assumptions, or simulated execution.
A finding that costs the author more to verify than it is worth is dropped.
"""

from __future__ import annotations

import re

NOT_CHEAP_MARKERS = re.compile(
    r"\btrace\b|\bassum\w*\b|consider the case|if an attacker|"
    r"\bsimulat\w*\b|\bimagine\b|run the code|execute\b|another file|"
    r"\bgrep the repo\b|across (the )?(repo|codebase)",
    re.IGNORECASE,
)


class CheapnessGate:
    """Verifies that candidate findings are cheaply observable and verifiable."""

    def __init__(self, markers: re.Pattern[str] = NOT_CHEAP_MARKERS) -> None:
        self.markers = markers

    def is_cheaply_verifiable(self, text: str) -> tuple[bool, str | None]:
        """Check if verification steps or check descriptions are cheaply verifiable.

        Returns:
            (is_cheap, match_reason)
            If is_cheap is False, match_reason provides the identified expensive marker.
        """
        if not text:
            return True, None

        match = self.markers.search(text)
        if match:
            marker = match.group(0)
            return False, f"not cheap to verify ({marker!r} in verification steps)"

        return True, None
