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
import pytest

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

    def test_sanitize_pr_body(self):
        """_sanitize_pr_body strips raw instruction headers and title format text."""
        from webhook_agent.webhook_agent import _sanitize_pr_body

        raw_body = (
            "# 🤖 Pull Request Description Template\n"
            "## 📋 Title Format\n"
            "[type] Brief description of changes\n"
            "## 🗒️ Description\n"
            "### What\nAdded new feature\n"
        )
        sanitized = _sanitize_pr_body(raw_body)
        assert "# 🤖 Pull Request Description Template" not in sanitized
        assert "## 📋 Title Format" not in sanitized
        assert "[type] Brief description of changes" not in sanitized
        assert "## 🗒️ Description" in sanitized
        assert "Added new feature" in sanitized

    def test_truncate_log_payload(self):
        """truncate_log_payload caps long payload strings for clean Cloud Logging output."""
        from webhook_agent.formatter import truncate_log_payload

        short_msg = "Short message"
        assert truncate_log_payload(short_msg, 300) == "Short message"

        long_msg = "A" * 500
        truncated = truncate_log_payload(long_msg, 100)
        assert len(truncated) < 500
        assert truncated.startswith("A" * 100)
        assert "... [truncated 400 chars]" in truncated

    def test_logger_hierarchy_named_loggers(self):
        """All webhook agent modules use unified 'webhook_agent.*' logger namespace."""
        from webhook_agent import (
            agent_core,
            enqueue,
            memory_service,
            processor,
            webhook_agent,
            worker,
        )

        assert processor.logger.name == "webhook_agent.processor"
        assert worker.logger.name == "webhook_agent.worker"
        assert webhook_agent.logger.name == "webhook_agent.agent"
        assert agent_core.logger.name == "webhook_agent.core"
        assert enqueue.logger.name == "webhook_agent.enqueue"
        assert memory_service.logger.name == "webhook_agent.memory"

    def test_fetch_repo_pr_template_fallback(self):
        """_fetch_repo_pr_template returns local pr_template if remote fetch fails."""
        from webhook_agent.webhook_agent import _fetch_repo_pr_template

        class MockRepo:
            def get_contents(self, path):
                raise Exception("Not found")

        class MockGithub:
            def get_repo(self, name):
                return MockRepo()

        template = _fetch_repo_pr_template(MockGithub(), "org/repo")
        assert "## 🗒️ Description" in template

    def test_fetch_repo_pr_template_dev_vs_prod(self):
        """_fetch_repo_pr_template selects dev vs prod template based on git diff changed files."""
        from webhook_agent.webhook_agent import _fetch_repo_pr_template

        class MockFile:
            def __init__(self, name, content):
                self.name = name
                self.decoded_content = content.encode("utf-8")

        class MockRepo:
            def get_contents(self, path):
                if path == ".github/PULL_REQUEST_TEMPLATE":
                    return [
                        MockFile("dev_pull_request_template.md", "DEV TEMPLATE"),
                        MockFile("prod_pull_request_template.md", "PROD TEMPLATE"),
                    ]
                raise Exception("Not found")

        class MockGithub:
            def get_repo(self, name):
                return MockRepo()

        # Dev-only changes
        dev_tpl = _fetch_repo_pr_template(
            MockGithub(), "org/repo", changed_files=["dev/auditor.py", "docs/README.md"]
        )
        assert dev_tpl == "DEV TEMPLATE"

        # Prod changes
        prod_tpl = _fetch_repo_pr_template(
            MockGithub(),
            "org/repo",
            changed_files=["src/hannibal/main.py", "dev/auditor.py"],
        )
        assert prod_tpl == "PROD TEMPLATE"

    def test_pr_lifecycle_events_allowed_for_llm_evaluation(self):
        """PR lifecycle events pass should_process_event for autonomous LLM evaluation."""
        assert (
            self.processor.should_process_event(
                _make_normalized("pull_request", action="closed")
            )
            is False
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

        from webhook_agent.webhook_types import ActionResult

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

    def test_agent_core_constructed_once_across_multiple_events(self):
        from unittest.mock import MagicMock, patch

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
        assert (
            "File: src/main.py (Line 42)"
            in payload["raw_payload"]["inline_code_context"]
        )

    def test_prefetch_commit_history(self):
        from unittest.mock import MagicMock
        from webhook_agent.processor import _prefetch_commit_history

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value

        mock_commit = MagicMock()
        mock_commit.commit.message = "feat: initial commit"
        mock_commit.sha = "abc123456"
        mock_commit.author.login = "developer"
        mock_pr.get_commits.return_value = [mock_commit]

        payload = {
            "canonical": "issue_comment.created",
            "raw_payload": {
                "issue": {"number": 10, "pull_request": {}},
                "comment": {"body": "Please /create PR description"},
            },
        }

        _prefetch_commit_history(mock_gh, "owner/repo", payload)
        assert "commit_history_summary" in payload["raw_payload"]
        assert "abc1234" in payload["raw_payload"]["commit_history_summary"]
        assert "developer" in payload["raw_payload"]["commit_history_summary"]

    def test_prefetch_previous_bot_reviews(self):
        from unittest.mock import MagicMock
        from webhook_agent.processor import _prefetch_previous_bot_reviews

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_pr = mock_repo.get_pull.return_value

        mock_review = MagicMock()
        mock_review.user.login = "hannibal-hub-agents[bot]"
        mock_review.state = "REQUEST_CHANGES"
        mock_review.body = "Please fix missing null check on line 42"
        mock_pr.get_reviews.return_value = [mock_review]

        payload = {
            "canonical": "pull_request.synchronize",
            "raw_payload": {
                "pull_request": {"number": 15},
            },
        }

        _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)
        assert "previous_bot_reviews" in payload["raw_payload"]
        assert "REQUEST_CHANGES" in payload["raw_payload"]["previous_bot_reviews"]
        assert "missing null check" in payload["raw_payload"]["previous_bot_reviews"]
