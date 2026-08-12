"""Tests for event routing, loop-avoidance, and dedupe rules in processor.py."""

from __future__ import annotations

import os
from typing import Any

# ---------------------------------------------------------------------------
# Environment setup for WebhookProcessor — MUST come before import
# ---------------------------------------------------------------------------
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_INSTALLATION_ID", "67890")
os.environ.setdefault("GITHUB_PRIVATE_KEY_PATH", "/dev/null")

from webhook_agent.processor import WebhookProcessor

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
        ev = _make_normalized(
            "pull_request", action="opened", sender_login="hannibal-hub-agents"
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_actor_by_type(self):
        """Bot events detected via sender.type == 'Bot' should be suppressed."""
        ev = _make_normalized(
            "issue_comment",
            action="created",
            sender_login="some-other-bot",
        )
        # A different bot should NOT be suppressed
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

    def test_pull_request_review_allowed(self):
        """pull_request_review submissions pass should_process_event for LLM evaluation."""
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="human-user",
        )
        assert self.processor.should_process_event(ev) is True

    def test_pr_lifecycle_events_allowed_for_llm_evaluation(self):
        """PR lifecycle events pass should_process_event for autonomous LLM evaluation."""
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request", action="closed")
            )
            is True
        )
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request", action="synchronize")
            )
            is True
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
            self.processor.should_process_event(
                _make_normalized("installation", action="created")
            )
            is False
        )
        assert (
            self.processor.should_process_event(
                _make_normalized("installation", action="deleted")
            )
            is False
        )

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

    def test_process_event_detail_preserves_code_formatting(self, caplog):
        """r.detail in process_event must preserve underscores, dots, and exact patch text."""
        import logging
        from unittest.mock import MagicMock, patch

        from webhook_agent.types import ActionResult

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
            caplog.at_level(logging.INFO),
        ):
            mock_agent = MagicMock()
            mock_agent.run.return_value = [mock_result]
            mock_core_cls.return_value = mock_agent

            self.processor.process_event(ev)

            assert "readarray -t modified_files *.ipynb" in caplog.text
            assert "modified files" not in caplog.text


class TestAddEyesReaction:
    def test_adds_reaction_to_issue_comment(self):
        from unittest.mock import MagicMock
        from webhook_agent.processor import _add_eyes_reaction

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_comment = mock_issue.get_comment.return_value

        payload = {
            "canonical": "issue_comment.created",
            "raw_payload": {
                "issue": {"number": 42},
                "comment": {"id": 101},
            },
        }

        _add_eyes_reaction(mock_gh, "owner/repo", payload)

        mock_gh.get_repo.assert_called_once_with("owner/repo")
        mock_repo.get_issue.assert_called_once_with(42)
        mock_issue.get_comment.assert_called_once_with(101)
        mock_comment.create_reaction.assert_called_once_with("eyes")

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
