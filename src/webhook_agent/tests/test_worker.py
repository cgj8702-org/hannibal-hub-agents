"""Tests for event routing, loop-avoidance, and dedupe rules in worker.py."""

from __future__ import annotations

from typing import Any

from webhook_agent.worker import (
    _is_bot_actor,
    _is_bot_comment_author,
    route_event,
    should_process_event,
    mark_processed,
    _processed_deliveries,
)


# ---------------------------------------------------------------------------
# Fixtures: normalized webhook payloads
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
    def test_pull_request_opened(self):
        ev = _make_normalized("pull_request", action="opened")
        assert route_event(ev) == "pull_request.opened"

    def test_pull_request_synchronize(self):
        ev = _make_normalized("pull_request", action="synchronize")
        assert route_event(ev) == "pull_request.synchronize"

    def test_pull_request_closed(self):
        ev = _make_normalized("pull_request", action="closed")
        assert route_event(ev) == "pull_request.closed"

    def test_pull_request_ready_for_review(self):
        ev = _make_normalized("pull_request", action="ready_for_review")
        assert route_event(ev) == "pull_request.ready_for_review"

    def test_pull_request_reopened(self):
        ev = _make_normalized("pull_request", action="reopened")
        assert route_event(ev) == "pull_request.reopened"

    def test_issue_comment_created(self):
        ev = _make_normalized("issue_comment", action="created")
        assert route_event(ev) == "issue_comment.created"

    def test_pull_request_review_comment_created(self):
        ev = _make_normalized("pull_request_review_comment", action="created")
        assert route_event(ev) == "pull_request_review_comment.created"

    def test_pull_request_review_submitted(self):
        ev = _make_normalized("pull_request_review", action="submitted")
        assert route_event(ev) == "pull_request_review.submitted"

    def test_pull_request_review_requested(self):
        ev = _make_normalized("pull_request", action="review_requested")
        assert route_event(ev) == "pull_request_review_requested"

    def test_label_created(self):
        ev = _make_normalized("label", action="created")
        assert route_event(ev) == "label.created"

    def test_label_deleted(self):
        ev = _make_normalized("label", action="deleted")
        assert route_event(ev) == "label.deleted"

    def test_installation_created(self):
        ev = _make_normalized("installation", action="created")
        assert route_event(ev) == "installation.created"

    def test_installation_deleted(self):
        ev = _make_normalized("installation", action="deleted")
        assert route_event(ev) == "installation.deleted"

    def test_ping(self):
        ev = _make_normalized("ping")
        assert route_event(ev) == "ping"

    def test_unknown_event(self):
        ev = _make_normalized("unknown_event_name")
        assert route_event(ev) == "unknown"

    def test_unknown_action(self):
        ev = _make_normalized("pull_request", action="unknown_action")
        assert route_event(ev) in ("pull_request.unknown_action",)


# ---------------------------------------------------------------------------
# Tests: _is_bot_actor
# ---------------------------------------------------------------------------


class TestIsBotActor:
    def test_bot_login(self):
        sender = {"login": "hannibal-hub-agents[bot]", "type": "Bot"}
        assert _is_bot_actor(sender) is True

    def test_human_login(self):
        sender = {"login": "test-user", "type": "User"}
        assert _is_bot_actor(sender) is False

    def test_none_sender(self):
        assert _is_bot_actor(None) is False

    def test_empty_dict(self):
        assert _is_bot_actor({}) is False


# ---------------------------------------------------------------------------
# Tests: _is_bot_comment_author
# ---------------------------------------------------------------------------


class TestIsBotCommentAuthor:
    def test_bot_comment_author(self):
        comment = {"user": {"login": "hannibal-hub-agents[bot]"}}
        assert _is_bot_comment_author(comment) is True

    def test_human_comment_author(self):
        comment = {"user": {"login": "test-user"}}
        assert _is_bot_comment_author(comment) is False

    def test_none_comment(self):
        assert _is_bot_comment_author(None) is False

    def test_no_user_key(self):
        comment = {"body": "hello"}
        assert _is_bot_comment_author(comment) is False


# ---------------------------------------------------------------------------
# Tests: should_process_event (loop-avoidance and dedupe)
# ---------------------------------------------------------------------------


class TestShouldProcessEvent:
    def setup_method(self):
        _processed_deliveries.clear()

    def test_normal_event_allowed(self):
        ev = _make_normalized("pull_request", action="opened")
        assert should_process_event(ev) is True

    def test_deduplicate_same_delivery(self):
        ev = _make_normalized("pull_request", action="opened", delivery_id="dup-001")
        assert should_process_event(ev) is True
        mark_processed("dup-001")
        assert should_process_event(ev) is False

    def test_suppress_bot_actor(self):
        ev = _make_normalized(
            "pull_request", action="opened", sender_login="hannibal-hub-agents[bot]"
        )
        assert should_process_event(ev) is False

    def test_suppress_bot_comment_author(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="hannibal-hub-agents[bot]",
        )
        assert should_process_event(ev) is False

    def test_suppress_bot_review_author(self):
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="hannibal-hub-agents[bot]",
        )
        assert should_process_event(ev) is False

    def test_human_comment_allowed(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="human-user",
        )
        assert should_process_event(ev) is True

    def test_human_review_allowed(self):
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="human-user",
        )
        assert should_process_event(ev) is True


# ---------------------------------------------------------------------------
# Tests: mark_processed
# ---------------------------------------------------------------------------


class TestMarkProcessed:
    def setup_method(self):
        _processed_deliveries.clear()

    def test_tracks_delivery_id(self):
        assert len(_processed_deliveries) == 0
        mark_processed("abc-123")
        assert "abc-123" in _processed_deliveries
        assert len(_processed_deliveries) == 1

    def test_dedupe_idempotent(self):
        mark_processed("same-id")
        mark_processed("same-id")
        assert len(_processed_deliveries) == 1
