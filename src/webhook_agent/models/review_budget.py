"""PR review comment budget, round decay, and convergence engine.

Adapted directly from adk-samples/.github/scripts/review_budget.py for hannibal-hub-agents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_LIFETIME_CAP = 25
DEFAULT_DECAY = 0.6
DEFAULT_MIN_ALLOWANCE = 1


@dataclass
class ReviewHistorySummary:
    round_number: int
    last_reviewed_sha: str
    total_posted_so_far: int
    previous_round_posted: int


def compute_round_allowance(
    round_number: int,
    previous_round_posted: int,
    total_posted_so_far: int,
    initial_budget: int = 5,
    lifetime_cap: int = DEFAULT_LIFETIME_CAP,
    decay: float = DEFAULT_DECAY,
    min_allowance: int = DEFAULT_MIN_ALLOWANCE,
) -> int:
    """Compute how many comments this round is allowed to post based on decay and cap.

    Prevents infinite cycles of re-reviewing by decaying comment allowances on subsequent
    pushes and enforcing a lifetime cap per pull request.
    """
    if total_posted_so_far >= lifetime_cap:
        return 0

    remaining_cap = lifetime_cap - total_posted_so_far

    if round_number <= 1:
        return min(initial_budget, remaining_cap)

    # Decayed basis from previous round
    basis = previous_round_posted if previous_round_posted > 0 else initial_budget
    allowance = max(min_allowance, math.floor(basis * (decay ** (round_number - 1))))
    return min(allowance, remaining_cap)


def should_suppress_round(
    total_posted_so_far: int,
    lifetime_cap: int = DEFAULT_LIFETIME_CAP,
) -> bool:
    """Check if review comments should be suppressed due to reaching lifetime cap."""
    return total_posted_so_far >= lifetime_cap


def summarise_review_history(
    bot_reviews: list[Any],
    bot_comments: list[Any],
    head_sha: str = "",
) -> ReviewHistorySummary:
    """Extract round number, last reviewed SHA, and cumulative comment counts from GitHub reviews.

    A 'round' corresponds to a distinct commit SHA that received a review.
    """
    valid_reviews = [
        r
        for r in bot_reviews
        if getattr(r, "commit_id", None) or (isinstance(r, dict) and r.get("commit_id"))
    ]

    commit_rounds: list[str] = []
    last_sha = ""

    for r in valid_reviews:
        sha = getattr(r, "commit_id", None) or (r.get("commit_id") if isinstance(r, dict) else "")
        if sha:
            last_sha = sha
            if sha not in commit_rounds:
                commit_rounds.append(sha)

    total_posted = len(bot_comments)
    round_number = len(commit_rounds) + 1

    # Calculate previous round comments count
    previous_round_posted = 0
    if commit_rounds:
        last_commit = commit_rounds[-1]
        previous_round_posted = sum(
            1
            for c in bot_comments
            if (
                getattr(c, "commit_id", None) or (c.get("commit_id") if isinstance(c, dict) else "")
            )
            == last_commit
        )

    return ReviewHistorySummary(
        round_number=round_number,
        last_reviewed_sha=last_sha,
        total_posted_so_far=total_posted,
        previous_round_posted=previous_round_posted,
    )
