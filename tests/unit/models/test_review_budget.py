"""Unit tests for PR review comment budget, round decay, and late-round blocker narrowing."""

from __future__ import annotations

import pytest

from webhook_agent.models.review_budget import (
    DEFAULT_BLOCKER_ONLY_AFTER_ROUND,
    DEFAULT_DECAY,
    DEFAULT_LIFETIME_CAP,
    DEFAULT_MIN_ALLOWANCE,
    compute_round_allowance,
    is_blocker_only_round,
    should_suppress_round,
    summarise_review_history,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_blocker_only_round() -> None:
    """Verify late-round blocker narrowing threshold."""
    assert DEFAULT_BLOCKER_ONLY_AFTER_ROUND == 2
    assert is_blocker_only_round(1) is False
    assert is_blocker_only_round(2) is False
    assert is_blocker_only_round(3) is True
    assert is_blocker_only_round(4) is True

    # Custom threshold
    assert is_blocker_only_round(2, threshold=1) is True
    assert is_blocker_only_round(2, threshold=3) is False


def test_compute_round_allowance_initial_round() -> None:
    """Verify initial round budget calculation."""
    allowance = compute_round_allowance(
        round_number=1,
        previous_round_posted=0,
        total_posted_so_far=0,
        initial_budget=5,
    )
    assert allowance == 5

    # Capped if near lifetime limit
    allowance_capped = compute_round_allowance(
        round_number=1,
        previous_round_posted=0,
        total_posted_so_far=23,
        initial_budget=5,
        lifetime_cap=25,
    )
    assert allowance_capped == 2


def test_compute_round_allowance_subsequent_rounds_decay() -> None:
    """Verify exponential decay on subsequent review rounds."""
    # Round 2: basis = 5 * 0.6^1 = 3
    allowance_r2 = compute_round_allowance(
        round_number=2,
        previous_round_posted=5,
        total_posted_so_far=5,
        initial_budget=5,
        decay=DEFAULT_DECAY,
    )
    assert allowance_r2 == 3

    # Round 3: basis = 3 * 0.6^2 = 3 * 0.36 = 1 (floor min_allowance = 1)
    allowance_r3 = compute_round_allowance(
        round_number=3,
        previous_round_posted=3,
        total_posted_so_far=8,
        initial_budget=5,
        decay=DEFAULT_DECAY,
        min_allowance=DEFAULT_MIN_ALLOWANCE,
    )
    assert allowance_r3 == 1


def test_compute_round_allowance_lifetime_cap() -> None:
    """Verify zero allowance when lifetime cap is met."""
    allowance = compute_round_allowance(
        round_number=4,
        previous_round_posted=1,
        total_posted_so_far=DEFAULT_LIFETIME_CAP,
        lifetime_cap=DEFAULT_LIFETIME_CAP,
    )
    assert allowance == 0
    assert should_suppress_round(DEFAULT_LIFETIME_CAP, DEFAULT_LIFETIME_CAP) is True
    assert should_suppress_round(DEFAULT_LIFETIME_CAP - 1, DEFAULT_LIFETIME_CAP) is False


def test_summarise_review_history() -> None:
    """Verify extraction of rounds, shas, and previous counts from GitHub review history."""
    bot_reviews = [
        {"commit_id": "sha-commit-1", "body": "Review 1"},
        {"commit_id": "sha-commit-1", "body": "Review 1 follow-up"},
        {"commit_id": "sha-commit-2", "body": "Review 2"},
    ]
    bot_comments = [
        {"commit_id": "sha-commit-1", "body": "Inline 1"},
        {"commit_id": "sha-commit-1", "body": "Inline 2"},
        {"commit_id": "sha-commit-2", "body": "Inline 3"},
    ]

    summary = summarise_review_history(bot_reviews, bot_comments, head_sha="sha-commit-3")
    assert summary.round_number == 3
    assert summary.last_reviewed_sha == "sha-commit-2"
    assert summary.total_posted_so_far == 3
    assert summary.previous_round_posted == 1
