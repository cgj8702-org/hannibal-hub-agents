"""Unit tests for trigger authorization gating in WebhookProcessor.should_process_event.

Tests gating based on author_association and exempt logins.
"""

from __future__ import annotations

import logging
import os
from typing import Any

os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_INSTALLATION_ID", "67890")
os.environ.setdefault("GITHUB_PRIVATE_KEY_PATH", "/dev/null")

import pytest

from webhook_agent.processor import WebhookProcessor

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _make_comment_event(
    body: str = "/review",
    author_association: str | None = None,
    sender_login: str = "random-user",
    author_login: str | None = None,
    delivery_id: str = "del-comment-123",
) -> dict[str, Any]:
    """Helper to build a normalized issue_comment webhook event."""
    author = author_login or sender_login
    comment_payload: dict[str, Any] = {
        "id": 101,
        "body": body,
        "user": {"login": author},
    }
    if author_association is not None:
        comment_payload["author_association"] = author_association

    return {
        "delivery_id": delivery_id,
        "event_name": "issue_comment",
        "action": "created",
        "sender": {"login": sender_login, "type": "User"},
        "installation": {"id": 12345},
        "repository": {"full_name": "owner/repo", "owner": {"login": "owner"}},
        "raw_payload": {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/42"},
            },
            "comment": comment_payload,
        },
    }


def _make_pr_event(
    action: str = "opened",
    author_association: str | None = None,
    sender_login: str = "random-user",
    author_login: str | None = None,
    delivery_id: str = "del-pr-123",
) -> dict[str, Any]:
    """Helper to build a normalized pull_request webhook event."""
    author = author_login or sender_login
    pr_payload: dict[str, Any] = {
        "number": 42,
        "title": "Test PR",
        "user": {"login": author},
    }
    if author_association is not None:
        pr_payload["author_association"] = author_association

    return {
        "delivery_id": delivery_id,
        "event_name": "pull_request",
        "action": action,
        "sender": {"login": sender_login, "type": "User"},
        "installation": {"id": 12345},
        "repository": {"full_name": "owner/repo", "owner": {"login": "owner"}},
        "raw_payload": {
            "action": action,
            "number": 42,
            "pull_request": pr_payload,
        },
    }


class TestTriggerAuthorization:
    """Tests for author_association gating and exempt logins in should_process_event."""

    def setup_method(self) -> None:
        """Initialize a fresh processor for each test."""
        self.processor = WebhookProcessor()

    def test_reproduce_unauthorized_none_commenter_ignored(self) -> None:
        """A NONE-association commenter saying '/review' must be ignored."""
        ev = _make_comment_event(
            body="/review",
            author_association="NONE",
            sender_login="stranger",
            delivery_id="del-none-01",
        )
        assert self.processor.should_process_event(ev) is False

    def test_owner_commenter_allowed(self) -> None:
        """An OWNER commenter saying '/review' must be allowed."""
        ev = _make_comment_event(
            body="/review",
            author_association="OWNER",
            sender_login="repo-owner",
        )
        assert self.processor.should_process_event(ev) is True

    def test_member_collaborator_and_contributor_allowed(self) -> None:
        """MEMBER, COLLABORATOR, and CONTRIBUTOR commenters must be allowed by default."""
        for role in ("MEMBER", "COLLABORATOR", "CONTRIBUTOR"):
            ev = _make_comment_event(
                body="/review",
                author_association=role,
                sender_login="teammate",
                delivery_id=f"del-{role}",
            )
            assert self.processor.should_process_event(ev) is True

    def test_first_timer_ignored_by_default(self) -> None:
        """FIRST_TIMER and FIRST_TIME_CONTRIBUTOR must be ignored by default."""
        for role in ("FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR"):
            ev = _make_comment_event(
                body="/review my PR",
                author_association=role,
                sender_login="external-contributor",
                delivery_id=f"del-{role}",
            )
            assert self.processor.should_process_event(ev) is False

    def test_pr_events_gated_by_author_association(self) -> None:
        """PR events must be gated based on pull_request.author_association."""
        ev_owner = _make_pr_event(action="opened", author_association="OWNER", sender_login="alice")
        assert self.processor.should_process_event(ev_owner) is True

        ev_none = _make_pr_event(
            action="opened",
            author_association="NONE",
            sender_login="attacker",
            delivery_id="del-pr-none",
        )
        assert self.processor.should_process_event(ev_none) is False

        ev_contributor = _make_pr_event(
            action="synchronize",
            author_association="CONTRIBUTOR",
            sender_login="contributor",
            delivery_id="del-pr-contrib",
        )
        assert self.processor.should_process_event(ev_contributor) is True

        ev_first_timer = _make_pr_event(
            action="opened",
            author_association="FIRST_TIME_CONTRIBUTOR",
            sender_login="stranger",
            delivery_id="del-pr-first-timer",
        )
        assert self.processor.should_process_event(ev_first_timer) is False

    def test_exempt_logins_case_insensitive(self) -> None:
        """Exempt logins (cgj8702, cgj8702-agents, dependabot[bot], google-labs-jules[bot]) bypass association check."""
        exempt_logins = [
            "cgj8702",
            "CGJ8702",
            "cgj8702-agents",
            "dependabot[bot]",
            "Dependabot[Bot]",
            "google-labs-jules[bot]",
        ]
        for login in exempt_logins:
            # Comment event with NONE association but exempt sender
            ev_comment = _make_comment_event(
                body="/review",
                author_association="NONE",
                sender_login=login,
                delivery_id=f"del-exempt-{login}",
            )
            assert self.processor.should_process_event(ev_comment) is True

            # PR event with NONE association but exempt author
            ev_pr = _make_pr_event(
                action="opened",
                author_association="NONE",
                sender_login="external-trigger",
                author_login=login,
                delivery_id=f"del-pr-exempt-{login}",
            )
            assert self.processor.should_process_event(ev_pr) is True

    def test_synthetic_events_without_association_allowed(self) -> None:
        """Synthetic events or events without author_association remain allowed."""
        ev_synthetic_pr = _make_pr_event(action="opened", author_association=None)
        assert self.processor.should_process_event(ev_synthetic_pr) is True

        ev_synthetic_comment = _make_comment_event(body="synthetic", author_association=None)
        assert self.processor.should_process_event(ev_synthetic_comment) is True

    def test_env_override_allowed_associations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TRIGGER_ALLOWED_ASSOCIATIONS env var overrides allowed roles at call time."""
        monkeypatch.setenv("TRIGGER_ALLOWED_ASSOCIATIONS", "CONTRIBUTOR,NONE")
        ev_none = _make_comment_event(
            body="test",
            author_association="NONE",
            sender_login="rando",
        )
        assert self.processor.should_process_event(ev_none) is True

        ev_owner = _make_comment_event(
            body="test",
            author_association="OWNER",
            sender_login="repo-owner-without-exempt",
        )
        assert self.processor.should_process_event(ev_owner) is False

    def test_env_override_exempt_logins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TRIGGER_EXEMPT_LOGINS env var overrides exempt logins at call time."""
        monkeypatch.setenv("TRIGGER_EXEMPT_LOGINS", "custom-vip-user")
        ev = _make_comment_event(
            body="test",
            author_association="NONE",
            sender_login="custom-vip-user",
        )
        assert self.processor.should_process_event(ev) is True

        ev_old_exempt = _make_comment_event(
            body="test",
            author_association="NONE",
            sender_login="cgj8702",
        )
        assert self.processor.should_process_event(ev_old_exempt) is False

    def test_logging_unauthorized_event(self, caplog: pytest.LogCaptureFixture) -> None:
        """Unauthorized events must log at INFO with delivery_id and association, never comment body."""
        secret_comment = "TOP_SECRET_PROMPT_INJECTION_BODY"
        ev = _make_comment_event(
            body=secret_comment,
            author_association="NONE",
            sender_login="malicious-actor",
            delivery_id="del-secret-audit-999",
        )
        with caplog.at_level(logging.INFO):
            result = self.processor.should_process_event(ev)

        assert result is False
        assert "del-secret-audit-999" in caplog.text
        assert "NONE" in caplog.text
        assert secret_comment not in caplog.text
