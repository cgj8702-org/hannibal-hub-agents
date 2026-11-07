"""Unit tests for CheapnessGate regex verification."""

from __future__ import annotations

import pytest

from webhook_agent.review.critic.cheapness_gate import CheapnessGate

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_cheapness_gate_permits_locally_observable_steps() -> None:
    gate = CheapnessGate()
    cheap_steps = [
        "Check line 42 for missing null check before accessing user.email",
        "Verify that timeout parameter is passed to requests.get()",
        "Inspect return type annotation on calculate_total()",
        "",
    ]
    for steps in cheap_steps:
        is_cheap, reason = gate.is_cheaply_verifiable(steps)
        assert is_cheap is True
        assert reason is None


@pytest.mark.parametrize(
    "expensive_phrase,phrase_in_steps",
    [
        ("trace", "Trace the caller in main.py to see where auth header originates"),
        ("assume", "Assume user_id is negative and evaluate downstream consequences"),
        ("consider the case", "Consider the case where redis server drops the connection"),
        ("if an attacker", "If an attacker intercepts the token payload via MITM"),
        ("simulate", "Simulate concurrent worker execution under high thread pool pressure"),
        ("imagine", "Imagine a scenario where the disk is completely full"),
        ("run the code", "Run the code with pytest -k test_e2e to verify"),
        ("execute", "Execute the function with invalid inputs to trigger exception"),
        ("another file", "Look in another file (config.py) to check default settings"),
        ("grep the repo", "Grep the repo for all usages of SECRET_KEY"),
        ("across codebase", "Check across codebase for conflicting route handlers"),
    ],
)
def test_cheapness_gate_rejects_expensive_verification(
    expensive_phrase: str, phrase_in_steps: str
) -> None:
    gate = CheapnessGate()
    is_cheap, reason = gate.is_cheaply_verifiable(phrase_in_steps)
    assert is_cheap is False
    assert reason is not None
    assert "not cheap to verify" in reason
    assert expensive_phrase.lower() in reason.lower()
