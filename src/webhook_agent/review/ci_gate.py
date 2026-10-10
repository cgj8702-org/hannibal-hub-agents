"""CI gate: review a pull request only after its CI has finished green.

Automatic reviews (PR opened / pushed / reopened / ready for review) are held back until every
check on the PR's head commit has completed successfully:

* CI still running            -> defer silently; ``check_suite.completed`` re-evaluates later.
* CI failed                   -> skip silently. A new push, or a green re-run, re-evaluates.
* CI green                    -> review.

Manual ``/review`` comments never pass through this gate.

The gate is stateless on purpose. Nothing is remembered between events: every decision is
recomputed from GitHub, so a worker restart cannot lose a deferred review.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import Any

from webhook_agent.constants import DEFAULT_REVIEW_WAIT_FOR_CI

logger = logging.getLogger("webhook_agent.ci_gate")

# Events that start an automatic review and are therefore subject to the gate.
GATED_EVENTS = frozenset(
    {
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.reopened",
        "pull_request.ready_for_review",
    }
)

# check_suite.conclusion values that count as "this suite is fine".
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# Completed check run / suite conclusions that block a review.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure"}
)
# Legacy commit status states that block a review.
FAILING_STATUS_STATES = frozenset({"failure", "error"})


class CIState(StrEnum):
    """Aggregate CI verdict for one commit."""

    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"


def is_ci_gate_enabled() -> bool:
    """True unless ``REVIEW_WAIT_FOR_CI`` is explicitly turned off."""
    value = os.environ.get("REVIEW_WAIT_FOR_CI", DEFAULT_REVIEW_WAIT_FOR_CI)
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def evaluate_ci(gh: Any, repo_name: str, head_sha: str) -> CIState:
    """Summarise every check run and commit status on ``head_sha``.

    Any failing signal wins over pending, and pending wins over pass. If GitHub cannot be
    queried the result is ``PENDING`` (do not review yet): a later ``check_suite.completed``
    event retries the decision, so a transient API error cannot cause an unchecked review.
    """
    try:
        repo = gh.get_repo(repo_name)
        commit = repo.get_commit(head_sha)

        pending = False
        signals = 0

        for run in commit.get_check_runs():
            signals += 1
            if (getattr(run, "status", "") or "").lower() != "completed":
                pending = True
                continue
            if (getattr(run, "conclusion", "") or "").lower() in FAILING_CONCLUSIONS:
                return CIState.FAIL

        combined = commit.get_combined_status()
        # GitHub reports state="pending" with zero statuses when none exist, so only
        # trust the combined state when at least one status is actually present.
        if getattr(combined, "total_count", 0):
            signals += 1
            state = (getattr(combined, "state", "") or "").lower()
            if state in FAILING_STATUS_STATES:
                return CIState.FAIL
            if state == "pending":
                pending = True

        if pending:
            return CIState.PENDING

        if signals == 0:
            # Nothing reported yet. If the repo has CI workflows, runs are still being
            # created for this commit; otherwise there is no CI to wait for.
            if repo.get_workflows().totalCount > 0:
                return CIState.PENDING
        return CIState.PASS
    except Exception as exc:
        logger.warning(
            "Could not determine CI state for %s@%s (%s); treating as pending",
            repo_name,
            head_sha[:7],
            exc,
        )
        return CIState.PENDING


def pr_numbers_for_suite(gh: Any, repo_name: str, check_suite: dict[str, Any]) -> list[int]:
    """Return open PR numbers a finished check suite belongs to.

    ``check_suite.pull_requests`` is empty for PRs from forks, so fall back to asking GitHub
    which pull requests contain the suite's head commit.
    """
    head_sha = check_suite.get("head_sha") or ""
    numbers: list[int] = []
    for item in check_suite.get("pull_requests") or []:
        try:
            numbers.append(int(item["number"]))
        except (KeyError, TypeError, ValueError):
            continue
    if numbers or not head_sha:
        return numbers
    try:
        commit = gh.get_repo(repo_name).get_commit(head_sha)
        return [int(pr.number) for pr in commit.get_pulls() if getattr(pr, "state", "") == "open"]
    except Exception as exc:
        logger.warning("Could not map commit %s to pull requests: %s", head_sha[:7], exc)
        return []


def last_bot_review_sha(pr: Any, head_sha: str) -> tuple[bool, str]:
    """Inspect the bot's reviews on ``pr``.

    Returns ``(already_reviewed_head, last_reviewed_sha)``. ``last_reviewed_sha`` is the commit
    of the bot's most recent review (``""`` if it has never reviewed this PR).
    """
    last = ""
    reviewed_head = False
    for review in pr.get_reviews():
        login = (getattr(getattr(review, "user", None), "login", "") or "").lower()
        if "hannibal-hub-agents" not in login:
            continue
        commit_id = getattr(review, "commit_id", "") or ""
        if commit_id:
            last = commit_id
            if commit_id == head_sha:
                reviewed_head = True
    return reviewed_head, last


def build_review_event(
    pr: Any, repo_name: str, head_sha: str, last_reviewed_sha: str, delivery_id: str
) -> dict[str, Any]:
    """Synthesize the pull_request event that a deferred review should have been.

    * Never reviewed before -> ``opened`` (full review).
    * Reviewed an earlier commit -> ``synchronize`` with ``before`` set to the last reviewed
      commit, so commits that were held back by the gate are still covered.
    """
    pr_raw = dict(pr.raw_data)
    repo_raw = dict(pr.base.repo.raw_data) if getattr(pr, "base", None) else {}
    repo_raw.setdefault("full_name", repo_name)
    sender = pr_raw.get("user") or {}

    action = "synchronize" if last_reviewed_sha else "opened"
    raw_payload: dict[str, Any] = {
        "action": action,
        "number": pr_raw.get("number"),
        "pull_request": pr_raw,
        "repository": repo_raw,
        "sender": sender,
    }
    if last_reviewed_sha:
        raw_payload["before"] = last_reviewed_sha
        raw_payload["after"] = head_sha

    return {
        "delivery_id": delivery_id,
        "event_name": "pull_request",
        "action": action,
        "sender": sender,
        "raw_payload": raw_payload,
        "repository": repo_raw,
    }


__all__ = [
    "FAILING_CONCLUSIONS",
    "GATED_EVENTS",
    "PASSING_CONCLUSIONS",
    "CIState",
    "build_review_event",
    "evaluate_ci",
    "is_ci_gate_enabled",
    "last_bot_review_sha",
    "pr_numbers_for_suite",
]
