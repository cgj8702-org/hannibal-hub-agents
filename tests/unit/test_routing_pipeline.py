"""Unit tests for the 3-Tier Webhook Routing Pipeline.

Verifies:
- Tier 0: Pure perimeter & worker drop guard (0 compute, 0 API calls).
- Tier 1: Deterministic programmatic actions (0 LLM calls).
- Tier 2: Conditional LLM delegation (strictly bounded).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.processor import Tier1Status, WebhookProcessor
from webhook_agent.review.ci_gate import CIState

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def processor() -> WebhookProcessor:
    proc = WebhookProcessor()
    mock_gh = MagicMock()
    proc._gh = mock_gh
    proc._gh_token_expires_at = None
    return proc


def _base_event(
    event_name: str,
    action: str,
    sender: str = "cgj8702",
    repo: str = "cgj8702-org/test-repo",
    extra_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
    }
    if extra_raw:
        raw.update(extra_raw)
    return {
        "event_name": event_name,
        "action": action,
        "delivery_id": f"del-{event_name}-{action}",
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
        "raw_payload": raw,
    }


class TestTier0DropGuard:
    def test_drops_untargeted_issue_opened(self, processor: WebhookProcessor) -> None:
        """Issue opened without mentioning the bot or requesting triage is dropped (record-only)."""
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 100,
                    "title": "Bug in parser",
                    "body": "There is a bug when parsing inputs.",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        assert processor.should_process_event(event) is False

    def test_allows_issue_opened_mentioning_bot(self, processor: WebhookProcessor) -> None:
        """Issue opened that tags @hannibal-hub-agents passes Tier 0."""
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 101,
                    "title": "Question for agent",
                    "body": "Hey @hannibal-hub-agents can you help triage this?",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        assert processor.should_process_event(event) is True

    def test_allows_issue_opened_with_triage_command(self, processor: WebhookProcessor) -> None:
        """Issue opened that includes /triage passes Tier 0."""
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 102,
                    "title": "Bug report /triage",
                    "body": "Need urgent triage.",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        assert processor.should_process_event(event) is True

    def test_allows_issue_opened_with_review_intent_keyword(
        self, processor: WebhookProcessor
    ) -> None:
        """Issue opened that requests /review passes Tier 0."""
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 103,
                    "title": "Need audit",
                    "body": "Please /review this issue proposal.",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        assert processor.should_process_event(event) is True

    def test_allows_partner_bot_issue_opened(self, processor: WebhookProcessor) -> None:
        """Issues filed by partner bots (e.g. Cline) pass Tier 0 per #441."""
        event = _base_event(
            "issues",
            "opened",
            sender="cline-cloud[bot]",
            extra_raw={
                "issue": {
                    "number": 104,
                    "title": "Automated task from Cline",
                    "body": "Task details here.",
                    "user": {"login": "cline-cloud[bot]"},
                    "author_association": "CONTRIBUTOR",
                }
            },
        )
        assert processor.should_process_event(event) is True

    def test_allows_issue_opened_when_auto_triage_env_enabled(
        self, processor: WebhookProcessor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AUTO_TRIAGE_NEW_ISSUES=1, untargeted issues also pass Tier 0."""
        monkeypatch.setenv("AUTO_TRIAGE_NEW_ISSUES", "1")
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 105,
                    "title": "Untargeted issue",
                    "body": "No bot mention.",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        assert processor.should_process_event(event) is True

    @pytest.mark.parametrize(
        "noise_event",
        ["star", "watch", "fork", "release", "sponsorship", "milestone"],
    )
    def test_drops_perimeter_noise_events(
        self, processor: WebhookProcessor, noise_event: str
    ) -> None:
        """Perimeter noise events are dropped in Tier 0."""
        event = _base_event(noise_event, "created")
        assert processor.should_process_event(event) is False

    def test_drops_pull_request_review_submitted(self, processor: WebhookProcessor) -> None:
        """Review submission events are dropped in Tier 0 to avoid loops."""
        event = _base_event(
            "pull_request_review",
            "submitted",
            extra_raw={
                "pull_request": {"number": 10, "state": "open"},
                "review": {"state": "approved"},
            },
        )
        assert processor.should_process_event(event) is False

    def test_pull_request_closed_marks_registry_and_drops(
        self, processor: WebhookProcessor
    ) -> None:
        """Closed PR event marks pr_closed_registry and drops in Tier 0."""
        repo = "cgj8702-org/test-repo"
        pr_number = 77
        event = _base_event(
            "pull_request",
            "closed",
            repo=repo,
            extra_raw={
                "pull_request": {"number": pr_number, "state": "closed"},
            },
        )
        assert processor.should_process_event(event) is False
        assert pr_closed_registry.is_closed(repo, pr_number) is True


class TestTier1ProgrammaticActions:
    def test_check_suite_completed_handled_programmatically(
        self, processor: WebhookProcessor
    ) -> None:
        """check_suite.completed is handled in Tier 1 with 0 LLM calls (TERMINAL)."""
        event = _base_event(
            "check_suite",
            "completed",
            extra_raw={
                "check_suite": {
                    "head_sha": "abc123456789",
                    "conclusion": "success",
                    "pull_requests": [],
                }
            },
        )
        event["canonical"] = "check_suite.completed"
        with patch.object(processor, "_handle_ci_completed") as mock_ci:
            status = processor._handle_tier_1_programmatic(event, "check_suite.completed", "del-ci")
            assert status == Tier1Status.TERMINAL
            mock_ci.assert_called_once_with(event)

    def test_base_branch_merge_sync_handled_programmatically(
        self, processor: WebhookProcessor
    ) -> None:
        """pull_request.synchronize from base branch merge is dropped in Tier 1 (TERMINAL)."""
        event = _base_event(
            "pull_request",
            "synchronize",
            extra_raw={
                "pull_request": {
                    "number": 50,
                    "state": "open",
                    "head": {"sha": "headsha123"},
                    "base": {"ref": "main"},
                }
            },
        )
        event["canonical"] = "pull_request.synchronize"
        with patch("webhook_agent.processor.is_base_branch_merge_sync", return_value=True):
            status = processor._handle_tier_1_programmatic(
                event, "pull_request.synchronize", "del-sync"
            )
            assert status == Tier1Status.TERMINAL

    def test_closed_or_merged_pr_terminates_in_tier_1(self, processor: WebhookProcessor) -> None:
        """PR target already closed terminates in Tier 1 (TERMINAL)."""
        event = _base_event(
            "pull_request",
            "opened",
            extra_raw={
                "pull_request": {
                    "number": 51,
                    "state": "closed",
                    "head": {"sha": "headsha123"},
                }
            },
        )
        event["canonical"] = "pull_request.opened"
        status = processor._handle_tier_1_programmatic(event, "pull_request.opened", "del-closed")
        assert status == Tier1Status.TERMINAL

    def test_failing_ci_terminates_in_tier_1(self, processor: WebhookProcessor) -> None:
        """Gated PR event with failing CI terminates in Tier 1 (TERMINAL)."""
        event = _base_event(
            "pull_request",
            "opened",
            extra_raw={
                "pull_request": {
                    "number": 52,
                    "state": "open",
                    "head": {"sha": "headsha123"},
                }
            },
        )
        event["canonical"] = "pull_request.opened"
        with (
            patch("webhook_agent.processor.is_ci_gate_enabled", return_value=True),
            patch("webhook_agent.processor.evaluate_ci", return_value=CIState.FAIL),
        ):
            status = processor._handle_tier_1_programmatic(
                event, "pull_request.opened", "del-ci-fail"
            )
            assert status == Tier1Status.TERMINAL

    def test_duplicate_head_review_terminates_in_tier_1(self, processor: WebhookProcessor) -> None:
        """PR commit that already has a bot review terminates in Tier 1 (TERMINAL)."""
        event = _base_event(
            "pull_request",
            "opened",
            extra_raw={
                "pull_request": {
                    "number": 53,
                    "state": "open",
                    "head": {"sha": "headsha123"},
                }
            },
        )
        event["canonical"] = "pull_request.opened"
        mock_repo = processor.gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value
        mock_pr.state = "open"
        mock_pr.head.sha = "headsha123"

        with (
            patch("webhook_agent.processor.is_ci_gate_enabled", return_value=False),
            patch("webhook_agent.processor.last_bot_review_sha", return_value=(True, "headsha123")),
        ):
            status = processor._handle_tier_1_programmatic(event, "pull_request.opened", "del-dup")
            assert status == Tier1Status.TERMINAL

    def test_preconditions_satisfied_advances_to_tier_2(self, processor: WebhookProcessor) -> None:
        """PR event passing all checks advances to Tier 2 (ADVANCE)."""
        event = _base_event(
            "pull_request",
            "opened",
            extra_raw={
                "pull_request": {
                    "number": 54,
                    "state": "open",
                    "head": {"sha": "headsha123"},
                }
            },
        )
        event["canonical"] = "pull_request.opened"
        mock_repo = processor.gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value
        mock_pr.state = "open"
        mock_pr.head.sha = "headsha123"

        with (
            patch("webhook_agent.processor.is_ci_gate_enabled", return_value=True),
            patch("webhook_agent.processor.evaluate_ci", return_value=CIState.PASS),
            patch("webhook_agent.processor.last_bot_review_sha", return_value=(False, None)),
            patch("webhook_agent.state.reconciliation.try_claim_reconciliation", return_value=True),
            patch("webhook_agent.processor.evaluate_dependency_fast_path", return_value=False),
        ):
            status = processor._handle_tier_1_programmatic(event, "pull_request.opened", "del-pass")
            assert status == Tier1Status.ADVANCE


class TestPipelineDispatch:
    def test_untargeted_issue_does_not_dispatch(self, processor: WebhookProcessor) -> None:
        """Untargeted issue opened terminates in Tier 0 and never reaches dispatch or agent."""
        event = _base_event(
            "issues",
            "opened",
            sender="cgj8702",
            extra_raw={
                "issue": {
                    "number": 200,
                    "title": "Untargeted issue",
                    "body": "No mention of bot.",
                    "user": {"login": "cgj8702"},
                    "author_association": "OWNER",
                }
            },
        )
        with patch.object(processor, "_dispatch_event") as mock_dispatch:
            processor.process_event(event)
            mock_dispatch.assert_not_called()

    def test_tier_1_terminal_does_not_dispatch_tier_2_llm(
        self, processor: WebhookProcessor
    ) -> None:
        """When Tier 1 returns TERMINAL, Tier 2 LLM delegation is never invoked."""
        event = _base_event("check_suite", "completed")
        with (
            patch.object(
                processor, "_handle_tier_1_programmatic", return_value=Tier1Status.TERMINAL
            ),
            patch.object(processor, "_dispatch_tier_2_llm") as mock_tier_2,
        ):
            processor._dispatch_event(event, "check_suite.completed", "del-1")
            mock_tier_2.assert_not_called()

    def test_tier_1_advance_dispatches_tier_2_llm(self, processor: WebhookProcessor) -> None:
        """When Tier 1 returns ADVANCE, Tier 2 LLM delegation is invoked."""
        event = _base_event("pull_request", "opened")
        with (
            patch.object(
                processor, "_handle_tier_1_programmatic", return_value=Tier1Status.ADVANCE
            ),
            patch.object(processor, "_dispatch_tier_2_llm") as mock_tier_2,
        ):
            processor._dispatch_event(event, "pull_request.opened", "del-2")
            mock_tier_2.assert_called_once_with(event, "pull_request.opened", "del-2")
