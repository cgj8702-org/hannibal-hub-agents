"""CI gate: automatic reviews wait for green CI and stay silent when CI fails."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from webhook_agent.processor import WebhookProcessor
from webhook_agent.review.ci_gate import (
    CIState,
    build_review_event,
    evaluate_ci,
    is_ci_gate_enabled,
    last_bot_review_sha,
    pr_numbers_for_suite,
)
from webhook_agent.review.proactive_service import (
    build_reconciliation_cache_key,
    clear_reconciliation_cache,
    is_reconciliation_claimed,
)

REPO = "cgj8702-org/test-repo"
SHA = "a" * 40
OLD_SHA = "b" * 40


def _run(status: str = "completed", conclusion: str | None = "success") -> SimpleNamespace:
    return SimpleNamespace(status=status, conclusion=conclusion)


def _gh(
    runs: list[SimpleNamespace] | None = None,
    combined: tuple[int, str] = (0, "pending"),
    workflows: int = 1,
) -> MagicMock:
    """Fake GitHub client answering the CI queries for ``SHA``."""
    gh = MagicMock()
    repo = gh.get_repo.return_value
    commit = repo.get_commit.return_value
    commit.get_check_runs.return_value = runs or []
    commit.get_combined_status.return_value = SimpleNamespace(
        total_count=combined[0], state=combined[1]
    )
    repo.get_workflows.return_value = SimpleNamespace(totalCount=workflows)
    return gh


def _pr(reviews: list[Any] | None = None, head_sha: str = SHA, state: str = "open") -> MagicMock:
    pr = MagicMock()
    pr.state = state
    pr.number = 7
    pr.head.sha = head_sha
    pr.raw_data = {
        "number": 7,
        "state": state,
        "title": "t",
        "head": {"sha": head_sha, "ref": "feat"},
        "base": {"ref": "main"},
        "user": {"login": "alice", "type": "User"},
    }
    pr.base.repo.raw_data = {"full_name": REPO}
    pr.get_reviews.return_value = reviews or []
    return pr


def _bot_review(commit_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(login="hannibal-hub-agents[bot]"), commit_id=commit_id
    )


# ---------------------------------------------------------------------------
# evaluate_ci
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "combined", "workflows", "expected"),
    [
        ([_run(), _run()], (0, "pending"), 1, CIState.PASS),
        ([_run(conclusion="skipped"), _run(conclusion="neutral")], (0, "pending"), 1, CIState.PASS),
        ([_run(), _run("in_progress", None)], (0, "pending"), 1, CIState.PENDING),
        ([_run("queued", None)], (0, "pending"), 1, CIState.PENDING),
        ([_run(conclusion="failure")], (0, "pending"), 1, CIState.FAIL),
        ([_run(conclusion="cancelled")], (0, "pending"), 1, CIState.FAIL),
        ([_run(conclusion="timed_out")], (0, "pending"), 1, CIState.FAIL),
        # A failure beats a still-running check: no point waiting on a doomed commit.
        ([_run(conclusion="failure"), _run("in_progress", None)], (0, "pending"), 1, CIState.FAIL),
        # Legacy commit statuses.
        ([_run()], (1, "failure"), 1, CIState.FAIL),
        ([_run()], (1, "error"), 1, CIState.FAIL),
        ([_run()], (1, "pending"), 1, CIState.PENDING),
        ([_run()], (1, "success"), 1, CIState.PASS),
        # "pending" with zero statuses is GitHub's default, not a real signal.
        ([_run()], (0, "pending"), 1, CIState.PASS),
        # No signals yet: wait if the repo has workflows, otherwise there is no CI.
        ([], (0, "pending"), 3, CIState.PENDING),
        ([], (0, "pending"), 0, CIState.PASS),
    ],
)
def test_evaluate_ci(
    runs: list[SimpleNamespace], combined: tuple[int, str], workflows: int, expected: CIState
) -> None:
    assert evaluate_ci(_gh(runs, combined, workflows), REPO, SHA) is expected


def test_evaluate_ci_treats_api_errors_as_pending() -> None:
    gh = MagicMock()
    gh.get_repo.side_effect = RuntimeError("boom")
    assert evaluate_ci(gh, REPO, SHA) is CIState.PENDING


def test_gate_is_on_by_default_and_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_WAIT_FOR_CI", raising=False)
    assert is_ci_gate_enabled() is True
    monkeypatch.setenv("REVIEW_WAIT_FOR_CI", "0")
    assert is_ci_gate_enabled() is False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_pr_numbers_come_from_the_suite_payload_first() -> None:
    gh = MagicMock()
    suite = {"head_sha": SHA, "pull_requests": [{"number": 7}, {"number": 9}]}
    assert pr_numbers_for_suite(gh, REPO, suite) == [7, 9]
    gh.get_repo.assert_not_called()


def test_pr_numbers_fall_back_to_open_prs_for_the_commit() -> None:
    gh = MagicMock()
    commit = gh.get_repo.return_value.get_commit.return_value
    commit.get_pulls.return_value = [
        SimpleNamespace(number=7, state="open"),
        SimpleNamespace(number=8, state="closed"),
    ]
    assert pr_numbers_for_suite(gh, REPO, {"head_sha": SHA, "pull_requests": []}) == [7]


def test_last_bot_review_sha_ignores_other_reviewers() -> None:
    other = SimpleNamespace(user=SimpleNamespace(login="alice"), commit_id=SHA)
    pr = _pr([other, _bot_review(OLD_SHA)])
    assert last_bot_review_sha(pr, SHA) == (False, OLD_SHA)
    pr = _pr([_bot_review(OLD_SHA), _bot_review(SHA)])
    assert last_bot_review_sha(pr, SHA) == (True, SHA)
    assert last_bot_review_sha(_pr([]), SHA) == (False, "")


def test_build_review_event_is_initial_when_never_reviewed() -> None:
    ev = build_review_event(_pr(), REPO, SHA, "", "d1")
    assert ev["action"] == "opened"
    assert "before" not in ev["raw_payload"]
    assert ev["raw_payload"]["pull_request"]["head"]["sha"] == SHA


def test_build_review_event_covers_commits_the_gate_held_back() -> None:
    ev = build_review_event(_pr(), REPO, SHA, OLD_SHA, "d1")
    assert ev["action"] == "synchronize"
    assert ev["raw_payload"]["before"] == OLD_SHA
    assert ev["raw_payload"]["after"] == SHA


# ---------------------------------------------------------------------------
# WebhookProcessor behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def processor(monkeypatch: pytest.MonkeyPatch) -> WebhookProcessor:
    monkeypatch.setenv("REVIEW_WAIT_FOR_CI", "1")
    monkeypatch.setenv("DRY_RUN", "1")  # skip network prefetch / reactions
    clear_reconciliation_cache()
    proc = WebhookProcessor()
    proc._agent_core = MagicMock()
    proc._agent_core.run.return_value = []
    yield proc
    clear_reconciliation_cache()


def _pr_event(action: str = "opened", delivery: str = "d-1") -> dict[str, Any]:
    return {
        "delivery_id": delivery,
        "event_name": "pull_request",
        "action": action,
        "sender": {"login": "alice", "type": "User"},
        "raw_payload": {
            "action": action,
            "repository": {"full_name": REPO},
            "pull_request": {
                "number": 7,
                "state": "open",
                "merged": False,
                "head": {"sha": SHA},
            },
        },
    }


def _suite_event(conclusion: str = "success", delivery: str = "c-1") -> dict[str, Any]:
    return {
        "delivery_id": delivery,
        "event_name": "check_suite",
        "action": "completed",
        "sender": {"login": "github-actions[bot]", "type": "Bot"},
        "raw_payload": {
            "action": "completed",
            "repository": {"full_name": REPO},
            "check_suite": {
                "head_sha": SHA,
                "conclusion": conclusion,
                "pull_requests": [{"number": 7}],
            },
        },
    }


def test_review_waits_while_ci_is_running(processor: WebhookProcessor) -> None:
    processor._gh = _gh([_run("in_progress", None)])
    processor.process_event(_pr_event())
    processor._agent_core.run.assert_not_called()
    # A deferred event must not burn the dedup claim, or the later review would be dropped.
    assert not is_reconciliation_claimed(build_reconciliation_cache_key(REPO, 7, SHA))


def test_review_is_skipped_silently_when_ci_failed(processor: WebhookProcessor) -> None:
    processor._gh = _gh([_run(conclusion="failure")])
    processor.process_event(_pr_event())
    processor._agent_core.run.assert_not_called()
    # Silent: nothing posted anywhere.
    processor._gh.get_repo.return_value.get_pull.assert_not_called()


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_review_runs_when_ci_is_green(processor: WebhookProcessor, action: str) -> None:
    processor._gh = _gh([_run()])
    processor.process_event(_pr_event(action))
    processor._agent_core.run.assert_called_once()


def test_gate_can_be_switched_off(
    processor: WebhookProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_WAIT_FOR_CI", "0")
    processor._gh = _gh([_run("in_progress", None)])
    processor.process_event(_pr_event())
    processor._agent_core.run.assert_called_once()


def test_manual_review_comment_ignores_ci(processor: WebhookProcessor) -> None:
    processor._gh = _gh([_run(conclusion="failure")])
    processor.process_event(
        {
            "delivery_id": "m-1",
            "event_name": "issue_comment",
            "action": "created",
            "sender": {"login": "alice", "type": "User"},
            "raw_payload": {
                "action": "created",
                "repository": {"full_name": REPO},
                "issue": {"number": 7, "pull_request": {"url": "x"}},
                "comment": {"body": "/review", "user": {"login": "alice", "type": "User"}},
            },
        }
    )
    processor._agent_core.run.assert_called_once()


def test_ci_completion_events_are_not_dropped_as_bot_noise(processor: WebhookProcessor) -> None:
    assert processor.should_process_event(_suite_event()) is True
    # Other CI chatter is still ignored.
    for name in ("check_run", "status"):
        ev = _suite_event()
        ev["event_name"] = name
        ev["delivery_id"] = f"x-{name}"
        ev["sender"] = {"login": "alice", "type": "User"}
        assert processor.should_process_event(ev) is False


def test_ci_completion_events_are_dropped_when_gate_is_off(
    processor: WebhookProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_WAIT_FOR_CI", "0")
    assert processor.should_process_event(_suite_event()) is False


def test_green_ci_starts_the_held_back_review(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    gh.get_repo.return_value.get_pull.return_value = _pr()
    processor._gh = gh
    processor.process_event(_suite_event())
    processor._agent_core.run.assert_called_once()
    started = processor._agent_core.run.call_args.args[0]
    assert started["canonical"] == "pull_request.opened"


def test_followup_review_uses_last_reviewed_commit_as_base(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    gh.get_repo.return_value.get_pull.return_value = _pr([_bot_review(OLD_SHA)])
    processor._gh = gh
    processor.process_event(_suite_event())
    started = processor._agent_core.run.call_args.args[0]
    assert started["canonical"] == "pull_request.synchronize"
    assert started["raw_payload"]["before"] == OLD_SHA


def test_ci_still_running_elsewhere_does_not_review(processor: WebhookProcessor) -> None:
    # This suite passed, but another check on the same commit is still running.
    gh = _gh([_run(), _run("in_progress", None)])
    gh.get_repo.return_value.get_pull.return_value = _pr()
    processor._gh = gh
    processor.process_event(_suite_event())
    processor._agent_core.run.assert_not_called()


def test_failed_suite_never_reviews_and_never_queries_github(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    processor._gh = gh
    processor.process_event(_suite_event(conclusion="failure"))
    processor._agent_core.run.assert_not_called()
    gh.get_repo.assert_not_called()


def test_stale_commit_is_not_reviewed(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    gh.get_repo.return_value.get_pull.return_value = _pr(head_sha=OLD_SHA)  # newer push exists
    processor._gh = gh
    processor.process_event(_suite_event())
    processor._agent_core.run.assert_not_called()


def test_closed_pr_is_not_reviewed(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    gh.get_repo.return_value.get_pull.return_value = _pr(state="closed")
    processor._gh = gh
    processor.process_event(_suite_event())
    processor._agent_core.run.assert_not_called()


def test_already_reviewed_commit_is_not_reviewed_again(processor: WebhookProcessor) -> None:
    gh = _gh([_run()])
    gh.get_repo.return_value.get_pull.return_value = _pr([_bot_review(SHA)])
    processor._gh = gh
    processor.process_event(_suite_event())
    processor._agent_core.run.assert_not_called()
