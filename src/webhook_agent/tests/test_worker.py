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
        # Simulate process_event adding the delivery_id to the set
        self.processor._processed_deliveries.add("dup-001")
        # Second call with same delivery should now be suppressed
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_actor(self):
        ev = _make_normalized(
            "pull_request", action="opened", sender_login="hannibal-hub-agents[bot]"
        )
        assert self.processor.should_process_event(ev) is False

    def test_suppress_bot_comment_author(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="hannibal-hub-agents[bot]",
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

    def test_human_comment_allowed(self):
        ev = _make_normalized(
            "issue_comment",
            action="created",
            include_comment=True,
            comment_author="human-user",
        )
        assert self.processor.should_process_event(ev) is True

    def test_human_review_allowed(self):
        ev = _make_normalized(
            "pull_request_review",
            action="submitted",
            include_review=True,
            comment_author="human-user",
        )
        assert self.processor.should_process_event(ev) is True
