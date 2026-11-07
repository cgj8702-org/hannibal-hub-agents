"""Deterministic Machine Critic Package for PR review falsification.

Exposes:
- CriticEngine: Orchestrator for adversarial candidate falsification and issue filtering.
- WindowMatcher: ASCII skeleton window line verification with +/- 5 line drift snapping.
- CheapnessGate: Deterministic regex gate against expensive multi-file or speculative steps.
- AddressabilityChecker: Diff hunk right-side boundary checker.
"""

from __future__ import annotations

from webhook_agent.review.critic.addressability import AddressabilityChecker
from webhook_agent.review.critic.cheapness_gate import NOT_CHEAP_MARKERS, CheapnessGate
from webhook_agent.review.critic.critic_engine import MIN_VERIFIED_CONFIDENCE, CriticEngine
from webhook_agent.review.critic.window_matcher import (
    MAX_DRIFT,
    MIN_PREFIX,
    WindowMatcher,
    norm,
)

__all__ = [
    "MAX_DRIFT",
    "MIN_PREFIX",
    "MIN_VERIFIED_CONFIDENCE",
    "NOT_CHEAP_MARKERS",
    "AddressabilityChecker",
    "CheapnessGate",
    "CriticEngine",
    "WindowMatcher",
    "norm",
]
