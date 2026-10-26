"""Tests for event routing, loop-avoidance, and dedupe rules in processor.py."""

from __future__ import annotations

import contextlib
import os
from typing import Any

# ---------------------------------------------------------------------------
# Environment setup for WebhookProcessor — MUST come before import
# ---------------------------------------------------------------------------
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_INSTALLATION_ID", "67890")
os.environ.setdefault("GITHUB_PRIVATE_KEY_PATH", "/dev/null")

import httpx
import pytest
from github import GithubException
from google.api_core import exceptions as gcp_exceptions

from webhook_agent.processor import WebhookProcessor

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.pubsub]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_normalized(
    event_name: str,
    action: str | None = None,
    sender_login: str = "test-user",
    delivery_id: str = "delivery-001",
    include_comment: bool = False,
    comment_author: str = "another-user",
    include_review: bool = False,
    review_state: str = "approved",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
    }
    if event_name == "pull_request":
        payload["number"] = 42
        payload["pull_request"] = {"number": 42, "title": "Test PR"}

    normalized: dict[str, Any] = {
        "delivery_id": delivery_id,
        "event_name": event_name,
        "action": action,
        "sender": {"login": sender_login, "type": "User"},
        "installation": {"id": 12345},
        "repository": {"full_name": "owner/repo", "owner": {"login": "owner"}},
        "raw_payload": payload,
    }

    if include_comment:
        normalized["raw_payload"]["comment"] = {
            "id": 999,
            "body": "Test comment",
            "user": {"login": comment_author},
        }

    if include_review:
        normalized["raw_payload"]["review"] = {
            "id": 888,
            "body": "Test review",
            "state": review_state,
            "user": {"login": comment_author},
        }

    if action:
        normalized["raw_payload"]["action"] = action

    return normalized


# ---------------------------------------------------------------------------
# Tests: route_event
# ---------------------------------------------------------------------------


class TestRouteEvent:
    def setup_method(self):
        self.processor = WebhookProcessor()

    def test_pull_request_opened(self):
        ev = _make_normalized("pull_request", action="opened")
        assert self.processor.route_event(ev) == "pull_request.opened"

    def test_pull_request_synchronize(self):
        ev = _make_normalized("pull_request", action="synchronize")
        assert self.processor.route_event(ev) == "pull_request.synchronize"

    def test_pull_request_closed(self):
        ev = _make_normalized("pull_request", action="closed")
        assert self.processor.route_event(ev) == "pull_request.closed"

    def test_pull_request_ready_for_review(self):
        ev = _make_normalized("pull_request", action="ready_for_review")
        assert self.processor.route_event(ev) == "pull_request.ready_for_review"

    def test_pull_request_reopened(self):
        ev = _make_normalized("pull_request", action="reopened")
        assert self.processor.route_event(ev) == "pull_request.reopened"

    def test_issue_comment_created(self):
        ev = _make_normalized("issue_comment", action="created")
        assert self.processor.route_event(ev) == "issue_comment.created"

    def test_pull_request_review_comment_created(self):
        ev = _make_normalized("pull_request_review_comment", action="created")
        assert self.processor.route_event(ev) == "pull_request_review_comment.created"

    def test_pull_request_review_submitted(self):
        ev = _make_normalized("pull_request_review", action="submitted")
        assert self.processor.route_event(ev) == "pull_request_review.submitted"

    def test_pull_request_review_requested(self):
        ev = _make_normalized("pull_request", action="review_requested")
        assert self.processor.route_event(ev) == "pull_request_review_requested"

    def test_label_created(self):
        ev = _make_normalized("label", action="created")
        assert self.processor.route_event(ev) == "label.created"

    def test_label_event_is_not_formal_review_eligible(self):
        from webhook_agent.review.writeback_policy import _is_formal_review_eligible

        assert _is_formal_review_eligible("label.created") is False

    def test_label_deleted(self):
        ev = _make_normalized("label", action="deleted")
        assert self.processor.route_event(ev) == "label.deleted"

    def test_installation_created(self):
        ev = _make_normalized("installation", action="created")
        assert self.processor.route_event(ev) == "installation.created"

    def test_installation_deleted(self):
        ev = _make_normalized("installation", action="deleted")
        assert self.processor.route_event(ev) == "installation.deleted"

    def test_ping(self):
        ev = _make_normalized("ping")
        assert self.processor.route_event(ev) == "ping"

    def test_unknown_event(self):
        ev = _make_normalized("unknown_event_name")
        assert self.processor.route_event(ev) == "unknown"

    def test_unknown_action(self):
        ev = _make_normalized("pull_request", action="unknown_action")
        assert self.processor.route_event(ev) in ("pull_request.unknown_action",)


# ---------------------------------------------------------------------------
# Tests: should_process_event (loop-avoidance and dedupe)
# ---------------------------------------------------------------------------


class TestShouldProcessEvent:
    def setup_method(self):
        self.processor = WebhookProcessor()

    def test_normal_event_allowed(self):
        ev = _make_normalized("pull_request", action="opened")
        assert self.processor.should_process_event(ev) is True

    def test_deduplicate_processed_twice(self):
        ev = _make_normalized("pull_request", action="opened", delivery_id="dup-001")
        assert self.processor.should_process_event(ev) is True
        # Simulate process_event adding the delivery_id to the dict
        self.processor._processed_deliveries["dup-001"] = None
        # Second call with same delivery should now be suppressed
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_actor(self):
        ev = _make_normalized(
            "pull_request", action="opened", sender_login="hannibal-hub-agents[bot]"
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_actor_without_suffix(self):
        """Bot events where sender.login lacks the [bot] suffix should be suppressed."""
        ev = _make_normalized("pull_request", action="opened", sender_login="hannibal-hub-agents")
        assert self.processor.should_process_event(ev) is False

    def test_external_bot_not_suppressed_by_type_alone(self):
        """Events from external bots (sender.type == 'Bot') are not suppressed by type alone;

        only our own bot identity is filtered.
        """
        ev = _make_normalized(
            "issue_comment",
            action="created",
            sender_login="some-other-bot",
        )
        # An external bot is NOT suppressed solely by type == 'Bot'
        ev["sender"] = {
            "login": "some-other-bot",
            "type": "Bot",
        }
        assert self.processor.should_process_event(ev) is True
        # But if login starts with the app slug and type is Bot, it IS matched
        ev["sender"] = {
            "login": "hannibal-hub-agents",
            "type": "Bot",
        }
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_comment_author(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="hannibal-hub-agents[bot]",
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_comment_author_without_suffix(self):
        """Comment author without [bot] suffix should be suppressed."""
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="hannibal-hub-agents",
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_review_author(self):
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="hannibal-hub-agents[bot]",
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_performed_via_github_app(self):
        """Events with performed_via_github_app matching our app should be suppressed."""
        ev = _make_normalized(
            "issue_comment",
            action="created",
            sender_login="some-other-user",
            include_comment=True,
            comment_author="some-other-user",
        )
        ev["raw_payload"]["performed_via_github_app"] = {
            "id": int(os.environ.get("GITHUB_APP_ID", "12345")),
            "slug": "hannibal-hub-agents",
        }
        assert self.processor.should_process_event(ev) is False

    def test_suppress_performed_via_github_app_in_comment(self):
        """Events with performed_via_github_app inside comment should be suppressed.

        This matches the actual GitHub webhook structure when a bot creates a comment.
        """
        ev = _make_normalized(
            "issue_comment",
            action="created",
            sender_login="human-user",
            include_comment=True,
            comment_author="human-user",
        )
        # In real webhooks, performed_via_github_app is nested inside comment
        ev["raw_payload"]["comment"]["performed_via_github_app"] = {
            "id": int(os.environ.get("GITHUB_APP_ID", "12345")),
            "slug": "hannibal-hub-agents",
        }
        assert self.processor.should_process_event(ev) is False

    def test_human_comment_allowed(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="human-user",
        )
        assert self.processor.should_process_event(ev) is True

    def test_suppress_dependabot_comment(self):
        """Comments mentioning @dependabot should be suppressed from processing."""
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="human-user",
        )
        ev["raw_payload"]["comment"]["body"] = "@dependabot recreate"
        assert self.processor.should_process_event(ev) is False

    def test_suppress_coderabbit_comment(self):
        """Comments mentioning @coderabbitai or @coderabbitai[bot] should be suppressed."""
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="human-user",
        )
        ev["raw_payload"]["comment"]["body"] = "@coderabbitai summary"
        assert self.processor.should_process_event(ev) is False

        ev["raw_payload"]["comment"]["body"] = "Hey @coderabbitai[bot] please review this"
        assert self.processor.should_process_event(ev) is False

    def test_suppress_deleted_comment(self):
        """Deleted comment events should be suppressed from processing."""
        ev = _make_normalized("issue_comment", action="deleted")
        assert self.processor.should_process_event(ev) is False

    def test_pull_request_review_submitted_suppressed(self):
        """pull_request_review submissions are suppressed to prevent self-review feedback loops."""
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="human-user",
        )
        assert self.processor.should_process_event(ev) is False

    @pytest.mark.regression
    def test_review_submitted_does_not_mark_open_pr_closed(self):
        """A review submission must NOT register an open PR in the closed registry.

        Regression guard for the #267 bug: `pull_request_review.submitted` shared a branch with
        `pull_request.closed` and called `mark_closed`, poisoning the in-memory registry so every
        later event on that open PR was skipped as 'closed or merged' for the registry TTL.
        """
        from webhook_agent.core.cancellation import pr_closed_registry

        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="human-user",
        )
        # Attach the PR + repository the real webhook delivers inside raw_payload, so the buggy
        # mark_closed line is genuinely reachable (it reads raw_payload["repository"]["full_name"]).
        ev["raw_payload"]["pull_request"] = {"number": 42, "state": "open"}
        ev["raw_payload"]["repository"] = {"full_name": "owner/repo"}

        # The event is still suppressed (loop avoidance unchanged)...
        assert self.processor.should_process_event(ev) is False
        # ...but the open PR must remain un-poisoned, so later events still process.
        assert pr_closed_registry.is_closed("owner/repo", 42) is False

    def test_closed_pr_event_still_marks_registry(self):
        """Genuinely closed PRs must still register so in-flight work short-circuits."""
        from webhook_agent.core.cancellation import pr_closed_registry

        ev = _make_normalized("pull_request", action="closed")
        ev["raw_payload"]["pull_request"] = {"number": 42, "state": "closed"}
        ev["raw_payload"]["repository"] = {"full_name": "owner/repo"}

        assert self.processor.should_process_event(ev) is False
        assert pr_closed_registry.is_closed("owner/repo", 42) is True
        # Clean up the singleton so it cannot leak into other tests.
        pr_closed_registry._closed.pop("owner/repo#42", None)

    @pytest.mark.regression
    def test_pr_alias_closed_event_still_marks_registry(self):
        """The `pr` event-name alias with action=closed must still register as closed.

        CodeRabbit #268 flagged that route_event's fallback returns "pr.closed" (not
        "pull_request.closed") for event_name="pr", so the closed-PR branch must accept the
        alias too or a pr-aliased closed event would fall through unregistered. GitHub's
        X-GitHub-Event header only ever sends "pull_request" today, so this is a defensive guard.
        """
        from webhook_agent.core.cancellation import pr_closed_registry

        ev = _make_normalized("pr", action="closed")
        ev["raw_payload"]["pull_request"] = {"number": 42, "state": "closed"}
        ev["raw_payload"]["repository"] = {"full_name": "owner/repo"}

        # route_event's fallback maps ("pr", "closed") -> "pr.closed".
        assert self.processor.route_event(ev) == "pr.closed"

        assert self.processor.should_process_event(ev) is False
        assert pr_closed_registry.is_closed("owner/repo", 42) is True
        # Clean up the singleton so it cannot leak into other tests.
        pr_closed_registry._closed.pop("owner/repo#42", None)

    def test_prefetch_pr_diff(self, monkeypatch):
        """_prefetch_pr_diff programmatically populates raw_payload['pr_diff']."""
        from webhook_agent.processor import _prefetch_pr_diff

        class MockFile:
            filename = "src/main.py"
            status = "modified"
            patch = "@@ -1 +1 @@\n-old\n+new"

        class MockPR:
            def get_files(self):
                return [MockFile()]

        class MockRepo:
            def get_pull(self, number):
                return MockPR()

        class MockGithub:
            def get_repo(self, name):
                return MockRepo()

        ev = _make_normalized("pull_request", action="opened")
        ev["canonical"] = "pull_request.opened"
        ev["raw_payload"]["pull_request"] = {"number": 123}

        _prefetch_pr_diff(MockGithub(), "org/repo", ev)

        assert "pr_diff" in ev["raw_payload"]
        assert "File: src/main.py" in ev["raw_payload"]["pr_diff"]
        assert "@@ -1 +1 @@" in ev["raw_payload"]["pr_diff"]

        assert ev["raw_payload"]["changed_files"] == ["src/main.py"]
        assert "uv.lock" not in ev["raw_payload"]["pr_diff"]

    def test_should_prefetch_diff_guardrail(self):
        """_should_prefetch_diff prevents context bloat on routine comments or non-review events."""
        from webhook_agent.processor import _should_prefetch_diff

        assert _should_prefetch_diff("pull_request.opened", {}) is True
        assert _should_prefetch_diff("pull_request.synchronize", {}) is True
        assert (
            _should_prefetch_diff(
                "issue_comment.created",
                {"comment": {"body": "Please /review this PR"}},
            )
            is True
        )
        # Routine comments or closed events should NOT pre-fetch diffs
        assert (
            _should_prefetch_diff(
                "issue_comment.created", {"comment": {"body": "Thanks for updating!"}}
            )
            is False
        )
        assert _should_prefetch_diff("pull_request.closed", {}) is False

    def test_truncate_log_payload(self):
        """truncate_log_payload safely truncates long payload strings for Cloud Logging output."""
        from webhook_agent.review.formatter import truncate_log_payload

        short_msg = "Short message"
        assert truncate_log_payload(short_msg, 300) == "Short message"

        long_msg = "A" * 500
        output = truncate_log_payload(long_msg, 100)
        assert len(output) < 500
        assert output.startswith("A" * 100)
        assert "[truncated 400 chars]" in output

    def test_logger_hierarchy_named_loggers(self):
        """All webhook agent modules use unified 'webhook_agent.*' logger namespace."""
        from webhook_agent import (
            processor,
            worker,
        )
        from webhook_agent.core import agent_definition

        assert processor.logger.name == "webhook_agent.processor"
        assert processor.core_logger.name == "webhook_agent.core"
        assert worker.logger.name == "webhook_agent.worker"
        assert agent_definition.logger.name == "webhook_agent.core.agent_definition"

    def test_pr_lifecycle_events_allowed_for_llm_evaluation(self):
        """PR lifecycle events pass should_process_event for autonomous LLM evaluation."""
        assert (
            self.processor.should_process_event(_make_normalized("pull_request", action="closed"))
            is False
        )
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request", action="synchronize")
            )
            is True
        )

    def test_pr_metadata_events_suppressed(self):
        """PR metadata events (labeled, assigned, milestoned, etc.) must be suppressed."""
        for act in (
            "labeled",
            "unlabeled",
            "assigned",
            "unassigned",
            "milestoned",
            "demilestoned",
            "review_request_removed",
            "auto_merge_enabled",
            "auto_merge_disabled",
            "locked",
            "unlocked",
            "converted_to_draft",
            "dequeued",
            "enqueued",
        ):
            ev = _make_normalized("pull_request", action=act)
            assert self.processor.should_process_event(ev) is False, (
                f"Action {act} should be suppressed"
            )

        # Also verify when canonical is set explicitly
        ev_canonical = {"canonical": "pull_request.labeled", "delivery_id": "test-labeled-1"}
        assert self.processor.should_process_event(ev_canonical) is False

        # Verify issue metadata events are also suppressed
        ev_issue_labeled = _make_normalized("issues", action="labeled")
        assert self.processor.should_process_event(ev_issue_labeled) is False
        ev_issue_unlabeled = _make_normalized("issues", action="unlabeled")
        assert self.processor.should_process_event(ev_issue_unlabeled) is False
        ev_issue_canonical = {"canonical": "issues.labeled", "delivery_id": "test-issue-labeled-1"}
        assert self.processor.should_process_event(ev_issue_canonical) is False

    def test_pr_code_and_review_events_allowed(self):
        """PR code change and review events must not be suppressed."""
        for act in ("opened", "reopened", "synchronize", "ready_for_review"):
            ev = _make_normalized("pull_request", action=act)
            assert self.processor.should_process_event(ev) is True, (
                f"Action {act} should be allowed"
            )

    def test_installation_events_ignored(self):
        """Installation lifecycle events should be filtered early."""
        assert (
            self.processor.should_process_event(
                _make_normalized("installation", action="new_permissions_accepted")
            )
            is False
        )
        assert (
            self.processor.should_process_event(_make_normalized("installation", action="created"))
            is False
        )
        assert (
            self.processor.should_process_event(_make_normalized("installation", action="deleted"))
            is False
        )

    def test_review_thread_events_ignored(self):
        """Review thread lifecycle events (resolved/unresolved) must be ignored."""
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request_review_thread", action="resolved")
            )
            is False
        )
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request_review_thread", action="unresolved")
            )
            is False
        )

    def test_workflow_events_ignored(self):
        """workflow_run and workflow_job events must be suppressed as unhandled CI noise."""
        for event_name in ("workflow_run", "workflow_job"):
            for action in ("requested", "in_progress", "completed"):
                ev = _make_normalized(event_name, action=action)
                assert self.processor.should_process_event(ev) is False

    def test_process_event_null_repository_does_not_crash(self, caplog):
        """Payloads with 'repository': null must not crash with AttributeError."""
        import logging
        from unittest.mock import patch

        ev = _make_normalized("issue_comment", action="created")
        ev["repository"] = None
        ev["raw_payload"]["repository"] = None

        with (
            patch(
                "webhook_agent.processor.load_cached_token",
                return_value=None,
            ),
            patch("webhook_agent.processor.load_private_key", return_value=b"key"),
            patch("webhook_agent.processor.generate_jwt", return_value="jwt"),
            patch(
                "webhook_agent.processor.get_installation_token",
                return_value=type("T", (), {"token": "tok"})(),
            ),
            patch("webhook_agent.processor.save_cached_token"),
            patch("webhook_agent.processor.Github"),
            patch("webhook_agent.processor.AgentCore"),
            caplog.at_level(logging.WARNING),
        ):
            self.processor.process_event(ev)
            assert "No repository found" in caplog.text

    def test_process_event_detail_preserves_code_formatting(self, caplog, monkeypatch):
        """r.detail in process_event must preserve underscores, dots, and exact patch text."""
        import logging
        from unittest.mock import MagicMock, patch

        from webhook_agent.webhook_types import ActionResult

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        ev = _make_normalized("pull_request", action="opened")
        mock_result = ActionResult(
            tool="execute_command",
            success=True,
            detail="Tool executed: {'result': 'file: githooks/pre-commit (modified)\npatch: readarray -t modified_files *.ipynb'}",
        )

        with (
            patch(
                "webhook_agent.processor.load_cached_token",
                return_value=MagicMock(token="fake"),
            ),
            patch("webhook_agent.processor.Github"),
            patch("webhook_agent.processor.AgentCore") as mock_core_cls,
            caplog.at_level(logging.DEBUG),
        ):
            mock_agent = MagicMock()
            mock_agent.run.return_value = [mock_result]
            mock_core_cls.return_value = mock_agent

            self.processor.process_event(ev)

            assert "readarray -t modified_files *.ipynb" in caplog.text
            assert "modified files" not in caplog.text


@pytest.mark.regression
class TestAgentCoreSingleton:
    """Regression test: AgentCore/WebhookAgent must be constructed once per
    WebhookProcessor and reused across events, not rebuilt per event — otherwise
    the ADK session/memory services are silently lost on every single webhook.
    """

    def _make_pr_opened_event(self, delivery_id: str, pr_number: int) -> dict:
        return {
            "delivery_id": delivery_id,
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "test-user", "type": "User"},
            "installation": {"id": 12345},
            "repository": {
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
            },
            "raw_payload": {
                "action": "opened",
                "number": pr_number,
                "pull_request": {"number": pr_number, "title": f"PR {pr_number}"},
            },
        }

    def test_agent_core_constructed_once_across_multiple_events(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        with (
            patch("webhook_agent.processor.load_cached_token") as mock_load_token,
            patch("webhook_agent.processor.Github") as mock_github_cls,
            patch("webhook_agent.processor.AgentCore") as mock_agent_core_cls,
        ):
            mock_load_token.return_value = MagicMock(token="fake-token")
            mock_github_cls.return_value = MagicMock()

            mock_agent_core_instance = MagicMock()
            mock_agent_core_instance.run.return_value = []
            mock_agent_core_cls.return_value = mock_agent_core_instance

            processor = WebhookProcessor()

            processor.process_event(self._make_pr_opened_event("delivery-001", 42))
            processor.process_event(self._make_pr_opened_event("delivery-002", 43))

            # The constructor must only run once, no matter how many events
            # are processed by this WebhookProcessor instance.
            assert mock_agent_core_cls.call_count == 1

            # Both events must have been dispatched through that SAME instance.
            assert mock_agent_core_instance.run.call_count == 2


class TestAddEyesReaction:
    def test_adds_reaction_to_issue_comment(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value

        payload = {
            "canonical": "issue_comment.created",
            "raw_payload": {
                "issue": {"number": 42},
                "comment": {"id": 101},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)

        mock_gh.get_repo.assert_called_once_with("owner/repo")
        mock_issue = mock_repo.get_issue.return_value
        mock_repo.get_issue.assert_called_once_with(42)
        mock_issue.get_comment.assert_called_once_with(101)
        mock_issue.get_comment.return_value.create_reaction.assert_called_once_with("eyes")

    def test_adds_reaction_to_pr_review_comment(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value
        mock_comment = mock_pr.get_review_comment.return_value

        payload = {
            "canonical": "pull_request_review_comment.created",
            "raw_payload": {
                "pull_request": {"number": 15},
                "comment": {"id": 202},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)

        mock_repo.get_pull.assert_called_once_with(15)
        mock_pr.get_review_comment.assert_called_once_with(202)
        mock_comment.create_reaction.assert_called_once_with("eyes")

    def test_adds_reaction_to_pull_request_opened(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value

        payload = {
            "canonical": "pull_request.opened",
            "raw_payload": {
                "pull_request": {"number": 112},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)

        mock_repo.get_issue.assert_called_once_with(112)
        mock_issue.create_reaction.assert_called_once_with("eyes")

    def test_ignores_pull_request_synchronize_events(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        payload = {
            "canonical": "pull_request.synchronize",
            "action": "synchronize",
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {"number": 112},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)
        mock_gh.get_repo.assert_not_called()

    def test_ignores_deleted_comment_events(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        payload = {
            "canonical": "issue_comment.deleted",
            "action": "deleted",
            "raw_payload": {
                "action": "deleted",
                "issue": {"number": 42},
                "comment": {"id": 101},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)
        mock_gh.get_repo.assert_not_called()

    def test_remove_eyes_reaction_via_reaction_object(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _remove_eyes_reaction

        mock_gh = MagicMock()
        mock_rx = MagicMock()
        payload = {"canonical": "issue_comment.created", "raw_payload": {}}

        removed = _remove_eyes_reaction(mock_gh, "owner/repo", payload, reaction=mock_rx)
        assert removed is True
        mock_rx.delete.assert_called_once()

    def test_remove_eyes_reaction_via_target_lookup(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _remove_eyes_reaction

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_comment = mock_issue.get_comment.return_value

        bot_reaction = MagicMock()
        bot_reaction.content = "eyes"
        bot_reaction.id = 555
        bot_reaction.user.login = "hannibal-hub-agents[bot]"

        mock_comment.get_reactions.return_value = [bot_reaction]

        payload = {
            "canonical": "issue_comment.created",
            "raw_payload": {
                "issue": {"number": 42},
                "comment": {"id": 101},
            },
        }

        removed = _remove_eyes_reaction(mock_gh, "owner/repo", payload)
        assert removed is True
        mock_comment.delete_reaction.assert_called_once_with(555)


class TestPreworkPipelines:
    def test_prefetch_inline_comment_context(self):
        from webhook_agent.processor import _prefetch_inline_comment_context

        payload = {
            "canonical": "pull_request_review_comment.created",
            "raw_payload": {
                "comment": {
                    "path": "src/main.py",
                    "line": 42,
                    "diff_hunk": "@@ -40,5 +40,5 @@\n-old\n+new",
                }
            },
        }

        _prefetch_inline_comment_context(None, "owner/repo", payload)
        assert "inline_code_context" in payload["raw_payload"]
        assert "File: src/main.py (Line 42)" in payload["raw_payload"]["inline_code_context"]

    def test_prefetch_previous_bot_reviews(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _prefetch_previous_bot_reviews

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value

        mock_review = MagicMock()
        mock_review.user.login = "hannibal-hub-agents[bot]"
        mock_review.state = "APPROVED"
        mock_review.body = "LGTM! Ready to merge."
        mock_pr.get_reviews.return_value = [mock_review]

        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "pull_request": {"number": 15},
            },
        }

        _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)
        assert "previous_bot_reviews" in payload["raw_payload"]
        assert "APPROVED" in payload["raw_payload"]["previous_bot_reviews"]
        mock_review.dismiss.assert_not_called()


class TestBaseBranchMergeSync:
    def test_is_base_branch_merge_sync_fast_path(self):
        """Verify that commit messages alone cannot spoof a base branch merge sync."""
        from unittest.mock import MagicMock

        from webhook_agent.processor import is_base_branch_merge_sync

        mock_gh = MagicMock()
        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {
                    "number": 125,
                    "head": {
                        "sha": "0abebcc",
                        "ref": "dependabot/uv/cryptography-50.0.1",
                    },
                    "base": {"ref": "main", "sha": "36acd0e"},
                },
                "commits": [
                    {
                        "id": "0abebcc",
                        "message": "Merge branch 'main' into dependabot/uv/cryptography-50.0.1",
                    }
                ],
            },
        }

        # Message shortcut deleted; without before_sha and valid API parents returns False
        assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is False

    def test_is_base_branch_merge_sync_api_check_two_parents(self):
        """Verify that multi-parent merge commits verified against base via API return True."""
        from unittest.mock import MagicMock

        from webhook_agent.processor import is_base_branch_merge_sync

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_commit = MagicMock()
        mock_commit.parents = [MagicMock(sha="parent1"), MagicMock(sha="parent2")]
        mock_repo.get_commit.return_value = mock_commit

        mock_base_comp = MagicMock(status="identical")
        mock_diff_comp = MagicMock(files=[MagicMock(filename="file.py", sha="blob1")])
        mock_repo.compare.side_effect = lambda base, head: (
            mock_base_comp if base == "main" else mock_diff_comp
        )
        mock_repo.get_contents.return_value = MagicMock(sha="blob1")

        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "action": "synchronize",
                "before": "parent1",
                "after": "0abebcc",
                "pull_request": {
                    "number": 125,
                    "head": {"sha": "0abebcc", "ref": "feature-branch"},
                    "base": {"ref": "main", "sha": "36acd0e"},
                },
            },
        }

        assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is True
        mock_gh.get_repo.assert_called_once_with("owner/repo")
        mock_repo.get_commit.assert_called_once_with("0abebcc")

    def test_is_base_branch_merge_sync_single_parent_returns_false(self):
        """Verify that single-parent commits return False and never bypass review."""
        from unittest.mock import MagicMock

        from webhook_agent.processor import is_base_branch_merge_sync

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_commit = MagicMock()
        mock_commit.parents = [MagicMock(sha="parent1")]
        mock_commit.commit.message = "feat: add real feature work"
        mock_repo.get_commit.return_value = mock_commit

        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {
                    "number": 125,
                    "head": {"sha": "normal123", "ref": "feature-branch"},
                    "base": {"ref": "main", "sha": "36acd0e"},
                },
            },
        }

        assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is False

    def test_is_base_branch_merge_sync_non_synchronize_returns_false(self):
        """Verify that non-synchronize events (e.g. pull_request.opened) return False."""
        from unittest.mock import MagicMock

        from webhook_agent.processor import is_base_branch_merge_sync

        mock_gh = MagicMock()
        payload = {
            "canonical": "pull_request.opened",
            "raw_payload": {
                "action": "opened",
                "pull_request": {
                    "number": 125,
                    "head": {"sha": "0abebcc", "ref": "feature-branch"},
                    "base": {"ref": "main", "sha": "36acd0e"},
                },
            },
        }

        assert is_base_branch_merge_sync(mock_gh, "owner/repo", payload) is False

    def test_prefetch_previous_bot_reviews_is_strictly_read_only(self):
        from unittest.mock import MagicMock

        from webhook_agent.processor import _prefetch_previous_bot_reviews

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value

        mock_review = MagicMock()
        mock_review.user.login = "hannibal-hub-agents[bot]"
        mock_review.state = "APPROVED"
        mock_review.body = "LGTM! Ready to merge."
        mock_pr.get_reviews.return_value = [mock_review]

        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {
                    "number": 125,
                    "head": {"sha": "c199559", "ref": "feature"},
                    "base": {"ref": "main", "sha": "07977df"},
                },
            },
        }

        _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)
        assert "previous_bot_reviews" in payload["raw_payload"]
        assert "APPROVED" in payload["raw_payload"]["previous_bot_reviews"]
        # Must never dismiss reviews as a side-effect during pre-fetching!
        mock_review.dismiss.assert_not_called()

    def test_process_event_suppresses_base_branch_merge_sync(self, caplog):
        """Verify that processor suppresses review execution when base branch merge sync is verified."""
        import logging
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        ev = {
            "delivery_id": "delivery-sync-001",
            "event_name": "pull_request",
            "action": "synchronize",
            "canonical": "pull_request.synchronize",
            "sender": {"login": "human", "type": "User"},
            "installation": {"id": 12345},
            "repository": {"full_name": "owner/repo", "owner": {"login": "owner"}},
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {
                    "number": 125,
                    "head": {
                        "sha": "0abebcc",
                        "ref": "dependabot/uv/cryptography-50.0.1",
                    },
                    "base": {"ref": "main", "sha": "36acd0e"},
                },
                "commits": [
                    {
                        "id": "0abebcc",
                        "message": "Merge branch 'main' into dependabot/uv/cryptography-50.0.1",
                    }
                ],
            },
        }

        with (
            patch(
                "webhook_agent.processor.load_cached_token",
                return_value=MagicMock(token="fake"),
            ),
            patch("webhook_agent.processor.Github"),
            patch(
                "webhook_agent.processor.is_base_branch_merge_sync",
                return_value=True,
            ),
            patch("webhook_agent.processor.AgentCore") as mock_core_cls,
            patch("webhook_agent.processor._add_eyes_reaction") as mock_eyes,
            caplog.at_level(logging.DEBUG),
        ):
            mock_agent = MagicMock()
            mock_core_cls.return_value = mock_agent

            processor.process_event(ev)

            mock_agent.run.assert_not_called()
            mock_eyes.assert_not_called()
            assert "Suppressed pull_request.synchronize for PR #125" in caplog.text

    def test_processor_gh_property(self):
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        with (
            patch(
                "webhook_agent.processor.load_cached_token",
                return_value=MagicMock(token="fake-token"),
            ),
            patch("webhook_agent.processor.Github") as mock_github_cls,
        ):
            client = processor.gh
            assert client == mock_github_cls.return_value
            mock_github_cls.assert_called_once()

    def test_processor_gh_refreshes_expired_installation_token(self):
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        expired = MagicMock(
            token="expired-token",
            expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        refreshed = MagicMock(
            token="refreshed-token",
            expires_at=(datetime.now(UTC) + timedelta(minutes=9)).isoformat(),
        )
        with (
            patch(
                "webhook_agent.processor.load_cached_token",
                side_effect=[expired, refreshed],
            ),
            patch("webhook_agent.processor.Github") as mock_github_cls,
        ):
            first_client = processor.gh
            second_client = processor.gh

        assert first_client == mock_github_cls.return_value
        assert second_client == mock_github_cls.return_value
        assert mock_github_cls.call_count == 2

    def test_processor_gh_401_invalidation_bypasses_cached_token(self):
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        cached = MagicMock(token="revoked-token", expires_at="2099-01-01T00:00:00Z")
        refreshed = MagicMock(token="fresh-token", expires_at="2099-01-01T00:00:00Z")
        with (
            patch("webhook_agent.processor.load_cached_token", return_value=cached) as load_cached,
            patch("webhook_agent.processor.load_private_key", return_value="pem"),
            patch("webhook_agent.processor.generate_jwt", return_value="jwt"),
            patch(
                "webhook_agent.processor.get_installation_token",
                return_value=refreshed,
            ) as get_installation_token,
            patch("webhook_agent.processor.save_cached_token"),
            patch("webhook_agent.processor.Github") as mock_github_cls,
        ):
            first_client = processor.gh
            processor.invalidate_github_client()
            second_client = processor.gh

        load_cached.assert_called_once_with(processor.installation_id)
        get_installation_token.assert_called_once_with("jwt", processor.installation_id)
        assert first_client == mock_github_cls.return_value
        assert second_client == mock_github_cls.return_value
        assert mock_github_cls.call_count == 2

    def test_worker_suppresses_google_genai_models_logger(self):
        import logging

        import webhook_agent.worker as worker_module

        assert worker_module is not None
        logger = logging.getLogger("google_genai.models")
        assert logger.level == logging.ERROR

    def test_worker_suppresses_adk_runner_duplicate_traceback_logger(self):
        """ADK Runner's per-attempt 'Root node failed' traceback must be clamped.

        The orchestrator already logs each Google error once at the retry
        boundary, so the duplicate ADK traceback must not reach the console.
        """
        import logging

        import webhook_agent.worker as worker_module

        assert worker_module is not None
        logger = logging.getLogger("google_adk.google.adk.runners")
        assert logger.level == logging.CRITICAL

    def test_process_event_deterministic_fast_path_approves_clean_dependabot_pr(self):
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        mock_gh = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        processor._gh = mock_gh
        payload = {
            "canonical": "pull_request.opened",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "dependabot[bot]"},
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "raw_payload": {
                "sender": {"login": "dependabot[bot]"},
                "repository": {"full_name": "cgj8702-org/hannibal-hub"},
                "pull_request": {
                    "number": 195,
                    "state": "open",
                    "head": {"ref": "dependabot/uv/pypdf-6.19.0", "sha": "1234567"},
                },
                "changed_files": ["uv.lock"],
                "pr_diff": (
                    "diff --git a/uv.lock b/uv.lock\n"
                    '[[package]]\nname = "pypdf"\n-version = "6.16.1"\n+version = "6.19.0"\n'
                ),
            },
        }

        processor._agent_core = MagicMock()

        with patch("webhook_agent.review.fast_path._submit_formal_review") as mock_submit_review:
            mock_submit_review.return_value = ("Approved PR #195", True)
            with patch.dict(os.environ, {"DRY_RUN": "0", "ENABLE_AUTOMATIC_PR_REVIEWS": "1"}):
                processor.process_event(payload)

            # Verified that fast-path submitted review without invoking agent.run
            mock_submit_review.assert_called_once()
            call_kwargs = mock_submit_review.call_args[1]
            assert call_kwargs["event"] == "APPROVE"
            assert "pypdf" in call_kwargs["body"]
            processor._agent_core.run.assert_not_called()

    def test_process_event_in_flight_deduplication_skips_duplicate_webhook(self, monkeypatch):
        from unittest.mock import MagicMock

        from webhook_agent.review.proactive_service import clear_reconciliation_cache

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        clear_reconciliation_cache()

        processor = WebhookProcessor()
        mock_gh = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        processor._gh = mock_gh
        processor._agent_core = MagicMock()
        processor._agent_core.run.return_value = []

        payload1 = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-unique-1",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "test-developer"},
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "raw_payload": {
                "pull_request": {
                    "number": 310,
                    "state": "open",
                    "head": {"ref": "feat/branch-1", "sha": "commit_sha_12345"},
                }
            },
        }

        # First delivery acquires claim and executes agent.run
        processor.process_event(payload1)
        assert processor._agent_core.run.call_count == 1

        payload2 = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-unique-2",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "test-developer"},
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "raw_payload": {
                "pull_request": {
                    "number": 310,
                    "state": "open",
                    "head": {"ref": "feat/branch-1", "sha": "commit_sha_12345"},
                }
            },
        }

        # Duplicate delivery with different delivery_id for the same commit is skipped
        processor.process_event(payload2)
        assert processor._agent_core.run.call_count == 1

    def test_process_event_proactive_reconciliation_is_not_self_suppressed(self, monkeypatch):
        from unittest.mock import MagicMock

        from webhook_agent.review.proactive_service import (
            build_reconciliation_cache_key,
            clear_reconciliation_cache,
            try_claim_reconciliation,
        )

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        clear_reconciliation_cache()

        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()
        processor._agent_core.run.return_value = []

        # Simulate proactive sweep acquiring claim prior to event dispatch
        cache_key = build_reconciliation_cache_key(
            "cgj8702-org/hannibal-hub", 311, "sha_proactive_99"
        )
        assert try_claim_reconciliation(cache_key) is True

        payload = {
            "canonical": "pull_request.opened",
            "delivery_id": "proactive-reconcile-311-sha_pro",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "test-developer"},
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "raw_payload": {
                "pull_request": {
                    "number": 311,
                    "state": "open",
                    "head": {"ref": "feat/reconcile-test", "sha": "sha_proactive_99"},
                }
            },
        }

        # Synthetic proactive delivery is not rejected by processor's in-flight check
        processor.process_event(payload)
        assert processor._agent_core.run.call_count == 1

    def test_process_event_releases_claim_on_agent_exception(self, monkeypatch):
        from unittest.mock import MagicMock

        import pytest

        from webhook_agent.review.proactive_service import (
            build_reconciliation_cache_key,
            clear_reconciliation_cache,
            is_reconciliation_claimed,
        )

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        clear_reconciliation_cache()

        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()
        processor._agent_core.run.side_effect = RuntimeError("Simulated crash")

        cache_key = build_reconciliation_cache_key("cgj8702-org/hannibal-hub", 312, "sha_crash_555")

        payload = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-crash-1",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "test-developer"},
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "raw_payload": {
                "pull_request": {
                    "number": 312,
                    "state": "open",
                    "head": {"ref": "feat/crash-test", "sha": "sha_crash_555"},
                }
            },
        }

        with pytest.raises(RuntimeError, match="Simulated crash"):
            processor.process_event(payload)

        # Claim must have been released upon error
        assert is_reconciliation_claimed(cache_key) is False

    def test_process_event_skips_prefetch_when_resumable_checkpoint_present(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "1")
        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()

        payload = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-resumed-1",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "developer"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "pull_request": {
                    "number": 100,
                    "state": "open",
                    "head": {"sha": "sha_resume_123"},
                }
            },
        }

        mock_checkpoint = {
            "status": "rate_limited",
            "pr_number": 100,
            "head_sha": "sha_resume_123",
            "pr_diff": "diff --git a/foo b/foo",
            "precompiled_dossier": "Cached Dossier Text",
            "changed_files": ["foo.py"],
        }

        with (
            patch(
                "webhook_agent.processor.review_checkpoint_manager.get_resumable",
                return_value=mock_checkpoint,
            ),
            patch("webhook_agent.processor._prefetch_pr_diff") as mock_prefetch_diff,
            patch(
                "webhook_agent.processor._prefetch_inline_comment_context"
            ) as mock_prefetch_inline,
            patch(
                "webhook_agent.processor._prefetch_previous_bot_reviews"
            ) as mock_prefetch_reviews,
            patch("webhook_agent.processor._add_eyes_reaction"),
        ):
            processor.process_event(payload)

            mock_prefetch_diff.assert_not_called()
            mock_prefetch_inline.assert_not_called()
            mock_prefetch_reviews.assert_not_called()
            assert payload.get("resumed_checkpoint") == mock_checkpoint
            assert payload.get("precompiled_dossier") == "Cached Dossier Text"
            assert payload["raw_payload"].get("pr_diff") == "diff --git a/foo b/foo"
            assert processor._agent_core.run.call_count == 1

    def test_process_event_skips_prefetch_when_pr_closed(self):
        """When PR payload indicates closed state, process_event must skip prefetch and agent execution."""
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()

        payload = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-closed-1",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "developer"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "pull_request": {
                    "number": 101,
                    "state": "closed",
                    "head": {"sha": "sha_closed_123"},
                }
            },
        }

        with patch("webhook_agent.processor._prefetch_pr_diff") as mock_prefetch_diff:
            processor.process_event(payload)
            mock_prefetch_diff.assert_not_called()
            processor._agent_core.run.assert_not_called()

    def test_process_event_defers_when_live_head_verification_fails(self, caplog):
        """When live-head verification lookup fails with an exception, process_event must defer rather than review stale data."""
        from unittest.mock import MagicMock, patch

        processor = WebhookProcessor()
        mock_gh = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.side_effect = RuntimeError("GitHub API connection error")
        mock_gh.get_repo.return_value = mock_repo
        processor._gh = mock_gh
        processor._agent_core = MagicMock()

        payload = {
            "canonical": "pull_request.opened",
            "delivery_id": "delivery-err-1",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "developer"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "pull_request": {
                    "number": 102,
                    "state": "open",
                    "head": {"sha": "sha_live_err"},
                }
            },
        }

        with (
            patch("webhook_agent.processor._prefetch_pr_diff") as mock_prefetch_diff,
            patch(
                "webhook_agent.processor.review_checkpoint_manager.get_resumable",
                return_value=None,
            ),
        ):
            processor.process_event(payload)
            mock_prefetch_diff.assert_not_called()
            processor._agent_core.run.assert_not_called()
            assert "Could not verify live PR head" in caplog.text
            assert "deferring to avoid reviewing stale data" in caplog.text

    def test_worker_main_resume_sweep_triggers_processor(self, monkeypatch):
        from unittest.mock import patch

        from webhook_agent.worker import main

        monkeypatch.setenv("ENABLE_REVIEW_RESUME", "1")
        monkeypatch.setenv("REVIEW_RESUME_INTERVAL_SECONDS", "0")

        mock_checkpoint = {
            "status": "rate_limited",
            "repo": "owner/repo",
            "pr_number": 77,
            "head_sha": "sha_res_77",
            "canonical": "pull_request.opened",
            "delivery_id": "orig-del-77",
            "precompiled_dossier": "Cached Dossier",
        }

        with (
            patch("webhook_agent.worker.setup_cloud_logging"),
            patch("webhook_agent.worker.WebhookProcessor") as mock_processor_cls,
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient") as mock_sub_cls,
            patch("webhook_agent.worker.pubsub_v1.PublisherClient"),
            patch(
                "webhook_agent.state.review_checkpoint.review_checkpoint_manager.list_resumable",
                return_value=[mock_checkpoint],
            ),
        ):
            mock_proc = mock_processor_cls.return_value
            mock_sub = mock_sub_cls.return_value
            # Break after first loop iteration by having pull raise KeyboardInterrupt
            mock_sub.pull.side_effect = KeyboardInterrupt()

            with contextlib.suppress(KeyboardInterrupt):
                main()

            assert mock_proc.process_event.call_count == 1
            call_payload = mock_proc.process_event.call_args[0][0]
            assert call_payload["canonical"] == "pull_request.opened"
            assert call_payload["repository"]["full_name"] == "owner/repo"
            assert call_payload["raw_payload"]["pull_request"]["number"] == 77
            assert call_payload["raw_payload"]["pull_request"]["head"]["sha"] == "sha_res_77"
            assert call_payload["precompiled_dossier"] == "Cached Dossier"

    def test_process_event_skips_pr_lifecycle_events_when_auto_review_disabled(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "0")
        monkeypatch.setenv("DRY_RUN", "1")

        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()

        for canonical, event_name, action in [
            ("pull_request.opened", "pull_request", "opened"),
            ("pull_request.synchronize", "pull_request", "synchronize"),
            ("pull_request.reopened", "pull_request", "reopened"),
            ("pull_request.ready_for_review", "pull_request", "ready_for_review"),
            ("pull_request_review_requested", "pull_request", "review_requested"),
        ]:
            payload = {
                "canonical": canonical,
                "event_name": event_name,
                "action": action,
                "sender": {"login": "developer", "type": "User"},
                "repository": {"full_name": "owner/repo"},
                "raw_payload": {
                    "action": action,
                    "pull_request": {
                        "number": 55,
                        "state": "open",
                        "head": {"sha": "abc1234"},
                    },
                },
            }
            processor.process_event(payload)

        processor._agent_core.run.assert_not_called()

    def test_process_event_runs_review_on_explicit_comment_when_auto_review_disabled(
        self, monkeypatch
    ):
        from unittest.mock import MagicMock

        monkeypatch.setenv("ENABLE_AUTOMATIC_PR_REVIEWS", "0")
        monkeypatch.setenv("DRY_RUN", "1")

        processor = WebhookProcessor()
        processor._gh = MagicMock()
        processor._agent_core = MagicMock()
        processor._agent_core.run.return_value = []

        payload = {
            "canonical": "issue_comment.created",
            "event_name": "issue_comment",
            "action": "created",
            "sender": {"login": "cgj8702", "type": "User"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "action": "created",
                "issue": {
                    "number": 55,
                    "state": "open",
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/55"},
                },
                "comment": {
                    "id": 999,
                    "body": "@hannibal-hub-agents /review please",
                    "user": {"login": "cgj8702"},
                },
            },
        }

        processor.process_event(payload)
        processor._agent_core.run.assert_called_once()


class TestPubSubLeaseExtender:
    def test_lease_extender_extends_deadline_periodically(self):
        import time
        from unittest.mock import MagicMock

        from webhook_agent.worker import _lease_extender

        subscriber = MagicMock()
        subscription_path = "projects/test-project/subscriptions/test-sub"
        ack_id = "ack-12345"

        with _lease_extender(
            subscriber=subscriber,
            subscription_path=subscription_path,
            ack_id=ack_id,
            ack_deadline_seconds=30,
            interval_seconds=0.02,
        ):
            time.sleep(0.06)

        assert subscriber.modify_ack_deadline.call_count >= 2
        call_kwargs = subscriber.modify_ack_deadline.call_args[1]["request"]
        assert call_kwargs["subscription"] == subscription_path
        assert call_kwargs["ack_ids"] == [ack_id]
        assert call_kwargs["ack_deadline_seconds"] == 30

    def test_lease_extender_stops_on_clean_exit(self):
        import threading
        import time
        from unittest.mock import MagicMock

        from webhook_agent.worker import _lease_extender

        subscriber = MagicMock()
        with _lease_extender(
            subscriber=subscriber,
            subscription_path="test-sub",
            ack_id="ack-1",
            interval_seconds=0.01,
        ):
            time.sleep(0.02)

        # Heartbeat thread should have finished and not be alive
        active_names = [t.name for t in threading.enumerate() if t.is_alive()]
        assert "PubSubLeaseExtender" not in active_names

    def test_lease_extender_stops_on_exception(self):
        import threading
        import time
        from unittest.mock import MagicMock

        from webhook_agent.worker import _lease_extender

        subscriber = MagicMock()
        with pytest.raises(ValueError, match="simulated failure"):
            with _lease_extender(
                subscriber=subscriber,
                subscription_path="test-sub",
                ack_id="ack-1",
                interval_seconds=0.01,
            ):
                time.sleep(0.02)
                raise ValueError("simulated failure")

        active_names = [t.name for t in threading.enumerate() if t.is_alive()]
        assert "PubSubLeaseExtender" not in active_names

    def test_lease_extender_ignores_modify_deadline_errors(self):
        import time
        from unittest.mock import MagicMock

        from webhook_agent.worker import _lease_extender

        subscriber = MagicMock()
        subscriber.modify_ack_deadline.side_effect = RuntimeError("network blip")

        # Context manager should not crash despite background errors
        with _lease_extender(
            subscriber=subscriber,
            subscription_path="test-sub",
            ack_id="ack-1",
            interval_seconds=0.01,
        ):
            time.sleep(0.03)

        assert subscriber.modify_ack_deadline.call_count >= 2


class TestWorkerReliability:
    """Tests for worker reliability, error handling, transient retries, and dead-letter queue."""

    def test_publish_dead_letter_success_returns_true(self):
        from unittest.mock import MagicMock

        from webhook_agent.worker import publish_dead_letter

        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = "msg-id"
        publisher.publish.return_value = future

        result = publish_dead_letter(publisher, "projects/test/topics/dlq", b"payload")
        assert result is True
        publisher.publish.assert_called_once_with("projects/test/topics/dlq", b"payload")

    def test_publish_dead_letter_failure_returns_false(self):
        from unittest.mock import MagicMock

        from webhook_agent.worker import publish_dead_letter

        publisher = MagicMock()
        future = MagicMock()
        future.result.side_effect = RuntimeError("DLQ publish failed")
        publisher.publish.return_value = future

        result = publish_dead_letter(publisher, "projects/test/topics/dlq", b"payload")
        assert result is False

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (gcp_exceptions.ServiceUnavailable("unavailable"), True),
            (gcp_exceptions.ResourceExhausted("exhausted"), True),
            (gcp_exceptions.DeadlineExceeded("deadline"), True),
            (gcp_exceptions.InternalServerError("internal"), True),
            (GithubException(429, {"message": "too many requests"}, {}), True),
            (GithubException(500, {"message": "internal server error"}, {}), True),
            (GithubException(502, {"message": "bad gateway"}, {}), True),
            (GithubException(503, {"message": "unavailable"}, {}), True),
            (GithubException(504, {"message": "gateway timeout"}, {}), True),
            (GithubException(403, {"message": "API rate limit exceeded"}, {}), True),
            (GithubException(403, {"message": "Forbidden"}, {}), False),
            (GithubException(404, {"message": "Not Found"}, {}), False),
            (httpx.TransportError("connection dropped"), True),
            (TimeoutError("timeout"), True),
            (ConnectionError("conn refused"), True),
            (ValueError("bad value"), False),
            (KeyError("missing key"), False),
            (TypeError("type mismatch"), False),
        ],
    )
    def test_is_transient(self, exc: Exception, expected: bool):
        from webhook_agent.worker import _is_transient

        assert _is_transient(exc) is expected

    def test_is_transient_genai_server_error(self):
        from webhook_agent.worker import _is_transient

        try:
            from google.genai.errors import ServerError

            err = ServerError(503, "Service unavailable")
            assert _is_transient(err) is True
        except ImportError:
            pass

    def test_worker_main_missing_env_key_error_logs_and_returns_3(self, caplog):
        import logging
        from unittest.mock import patch

        from webhook_agent.worker import main as worker_main

        with patch("webhook_agent.worker.WebhookProcessor", side_effect=KeyError("MISSING_ENV")):
            with caplog.at_level(logging.ERROR):
                exit_code = worker_main()
            assert exit_code == 3
            assert "Missing required environment variable: 'MISSING_ENV'" in caplog.text

    def test_worker_dlq_publish_failure_leaves_unacked(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setenv("PUBSUB_DEAD_LETTER_TOPIC", "projects/test/topics/dlq")

        mock_processor = MagicMock()
        mock_processor.process_event.side_effect = RuntimeError("Fatal processor error")

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        mock_publisher = MagicMock()
        future = MagicMock()
        future.result.side_effect = RuntimeError("Pub/Sub DLQ failure")
        mock_publisher.publish.return_value = future

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient", return_value=mock_publisher),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        # Dead letter publish failed -> message MUST NOT be acked
        mock_subscriber.acknowledge.assert_not_called()

    def test_worker_dlq_publish_success_acks(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setenv("PUBSUB_DEAD_LETTER_TOPIC", "projects/test/topics/dlq")

        mock_processor = MagicMock()
        mock_processor.process_event.side_effect = RuntimeError("Fatal processor error")

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        mock_publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = "dlq-msg-id"
        mock_publisher.publish.return_value = future

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient", return_value=mock_publisher),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        # Dead letter publish succeeded -> message acked
        mock_subscriber.acknowledge.assert_called_once_with(
            request={
                "subscription": "projects/cgj8702-webhook-agent/subscriptions/webhooks-sub",
                "ack_ids": ["ack-1234"],
            }
        )

    def test_worker_transient_error_retried_and_succeeds(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setattr("webhook_agent.worker._RETRY_DELAYS", (0.0, 0.0))

        mock_processor = MagicMock()
        # Fails once with transient TimeoutError, then succeeds
        mock_processor.process_event.side_effect = [TimeoutError("Transient timeout"), None]

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient"),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        assert mock_processor.process_event.call_count == 2
        mock_subscriber.acknowledge.assert_called_once_with(
            request={
                "subscription": "projects/cgj8702-webhook-agent/subscriptions/webhooks-sub",
                "ack_ids": ["ack-1234"],
            }
        )

    def test_worker_transient_error_exhausted_goes_to_dlq(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setattr("webhook_agent.worker._RETRY_DELAYS", (0.0, 0.0))
        monkeypatch.setenv("PUBSUB_DEAD_LETTER_TOPIC", "projects/test/topics/dlq")

        mock_processor = MagicMock()
        # Fails all 3 attempts with transient TimeoutError
        mock_processor.process_event.side_effect = TimeoutError("Transient timeout")

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        mock_publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = "dlq-msg-id"
        mock_publisher.publish.return_value = future

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient", return_value=mock_publisher),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        assert mock_processor.process_event.call_count == 3
        mock_publisher.publish.assert_called_once_with("projects/test/topics/dlq", mock_msg.data)
        mock_subscriber.acknowledge.assert_called_once()

    def test_worker_non_transient_error_goes_to_dlq_without_retry(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setenv("PUBSUB_DEAD_LETTER_TOPIC", "projects/test/topics/dlq")

        mock_processor = MagicMock()
        mock_processor.process_event.side_effect = ValueError("Fatal invalid format")

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        mock_publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = "dlq-msg-id"
        mock_publisher.publish.return_value = future

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient", return_value=mock_publisher),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        assert mock_processor.process_event.call_count == 1
        mock_publisher.publish.assert_called_once_with("projects/test/topics/dlq", mock_msg.data)
        mock_subscriber.acknowledge.assert_called_once()

    def test_worker_proactive_sweep_overlapping_skipped(self, monkeypatch, caplog):
        import logging
        import threading
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        monkeypatch.setenv("ENABLE_PROACTIVE_SWEEP", "1")

        mock_processor = MagicMock()
        mock_subscriber = MagicMock()

        alive_thread = MagicMock(spec=threading.Thread)
        alive_thread.is_alive.return_value = True

        current_time = [500.0]

        def fake_pull(*args, **kwargs):
            if current_time[0] < 900.0:
                current_time[0] = 900.0
                raise gcp_exceptions.DeadlineExceeded("deadline")
            raise KeyboardInterrupt()

        mock_subscriber.pull.side_effect = fake_pull

        with (
            patch("webhook_agent.worker.setup_cloud_logging"),
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient"),
            patch("time.time", side_effect=lambda: current_time[0]),
            patch("threading.Thread", return_value=alive_thread) as mock_thread_cls,
            caplog.at_level(logging.WARNING),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

            # Filter calls for ProactiveSweepWorker
            sweep_thread_calls = [
                c
                for c in mock_thread_cls.call_args_list
                if c.kwargs.get("name") == "ProactiveSweepWorker"
            ]
            assert len(sweep_thread_calls) == 1
            assert any("previous sweep still alive" in rec.message for rec in caplog.records)

    def test_processor_exception_unmarks_delivery_id(self):
        from unittest.mock import patch

        processor = WebhookProcessor()
        payload = _make_normalized("pull_request", action="opened", delivery_id="deliv-err-1")

        with patch.object(
            processor, "_dispatch_event", side_effect=RuntimeError("processing blew up")
        ):
            with pytest.raises(RuntimeError, match="processing blew up"):
                processor.process_event(payload)

        # delivery_id should be removed from _processed_deliveries
        assert "deliv-err-1" not in processor._processed_deliveries

    def test_processor_retry_not_suppressed_as_duplicate(self):
        from unittest.mock import patch

        processor = WebhookProcessor()
        payload = _make_normalized("pull_request", action="opened", delivery_id="deliv-retry-1")

        # First attempt raises an error
        with patch.object(
            processor, "_dispatch_event", side_effect=RuntimeError("transient error")
        ):
            with pytest.raises(RuntimeError):
                processor.process_event(payload)

        # Retry should NOT be suppressed as duplicate by should_process_event
        assert processor.should_process_event(payload) is True

        # Second attempt succeeds
        with patch.object(processor, "_dispatch_event", return_value=None):
            processor.process_event(payload)

        # Now it IS marked as processed and duplicate suppressed
        assert "deliv-retry-1" in processor._processed_deliveries
        assert processor.should_process_event(payload) is False

    def test_worker_pull_error_branches(self):
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        mock_processor = MagicMock()
        mock_subscriber = MagicMock()

        # Simulate DeadlineExceeded, GoogleAPICallError, generic Exception, empty received_messages, then exit
        empty_response = MagicMock()
        empty_response.received_messages = []

        mock_subscriber.pull.side_effect = [
            gcp_exceptions.DeadlineExceeded("timeout"),
            gcp_exceptions.GoogleAPICallError("api err"),
            Exception("generic pull err"),
            empty_response,
            KeyboardInterrupt(),
        ]

        with (
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient"),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        assert mock_subscriber.pull.call_count == 5

    def test_setup_cloud_logging_success(self):
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import setup_cloud_logging

        mock_client = MagicMock()

        with patch("google.cloud.logging.Client", return_value=mock_client):
            setup_cloud_logging()
            mock_client.setup_logging.assert_called_once()

    def test_setup_cloud_logging_failure(self, caplog):
        import logging
        from unittest.mock import patch

        from webhook_agent.worker import setup_cloud_logging

        with patch.dict("sys.modules", {"google.cloud.logging": None}):
            with caplog.at_level(logging.WARNING):
                setup_cloud_logging()
            assert "Could not initialize Google Cloud Logging handler" in caplog.text

    def test_worker_clean_message_ack_no_dlq(self, monkeypatch):
        import json
        from unittest.mock import MagicMock, patch

        from webhook_agent.worker import main as worker_main

        # Empty string disables dead-letter topic
        monkeypatch.setenv("PUBSUB_DEAD_LETTER_TOPIC", "")

        mock_processor = MagicMock()
        mock_processor.process_event.side_effect = RuntimeError("Fatal processor error")

        mock_subscriber = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-1234"
        mock_msg.data = json.dumps({"test": "data"}).encode()
        received_msg = MagicMock()
        received_msg.message = mock_msg
        received_msg.ack_id = "ack-1234"

        mock_response = MagicMock()
        mock_response.received_messages = [received_msg]
        mock_subscriber.pull.side_effect = [mock_response, KeyboardInterrupt()]

        with (
            patch("webhook_agent.worker.setup_cloud_logging"),
            patch("webhook_agent.worker.WebhookProcessor", return_value=mock_processor),
            patch("webhook_agent.worker.pubsub_v1.SubscriberClient", return_value=mock_subscriber),
            patch("webhook_agent.worker.pubsub_v1.PublisherClient"),
        ):
            with contextlib.suppress(KeyboardInterrupt):
                worker_main()

        # Without DLQ, message should not be acked on failure to allow retry
        mock_subscriber.acknowledge.assert_not_called()
