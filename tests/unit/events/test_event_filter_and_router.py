"""Unit tests for the webhook_agent.events package (event_filter and tier1_router)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.events import (
    SUPPRESSED_EVENT_NAMES,
    SUPPRESSED_PR_METADATA_ACTIONS,
    Tier1Status,
    filter_tier_0,
    handle_ci_completed,
    handle_tier_1_programmatic,
    is_actionable_issue_opened,
    review_if_ci_green,
    route_event,
    should_process_event,
)
from webhook_agent.review.ci_gate import CIState

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_events_reexports():
    """Verify webhook_agent.events cleanly re-exports all filter and router symbols."""
    assert "check_suite" in SUPPRESSED_EVENT_NAMES
    assert "labeled" in SUPPRESSED_PR_METADATA_ACTIONS
    assert Tier1Status.TERMINAL.value == "terminal"
    assert Tier1Status.ADVANCE.value == "advance"


def test_route_event_canonical_mappings():
    """Verify route_event maps GitHub event tuples to canonical strings."""
    assert route_event({"event_name": "ping"}) == "ping"
    assert route_event({"event_name": "push"}) == "push"
    assert route_event({"event_name": "pull_request", "action": "opened"}) == "pull_request.opened"
    assert (
        route_event({"event_name": "pull_request", "action": "synchronize"})
        == "pull_request.synchronize"
    )
    assert route_event({"event_name": "pull_request", "action": "closed"}) == "pull_request.closed"
    assert (
        route_event({"event_name": "pull_request", "action": "ready_for_review"})
        == "pull_request.ready_for_review"
    )
    assert (
        route_event({"event_name": "issue_comment", "action": "created"}) == "issue_comment.created"
    )
    assert (
        route_event({"event_name": "pull_request_review_comment", "action": "created"})
        == "pull_request_review_comment.created"
    )
    assert (
        route_event({"event_name": "pull_request", "action": "review_requested"})
        == "pull_request_review_requested"
    )
    assert route_event({"event_name": "custom", "action": "fired"}) == "custom.fired"
    assert route_event({"event_name": "custom"}) == "unknown"


def test_is_actionable_issue_opened_rules():
    """Verify is_actionable_issue_opened only accepts targeted or command-driven issues."""
    # Partner bot
    assert is_actionable_issue_opened({"sender": {"login": "cline-cloud"}}) is True

    # Bot mention
    assert (
        is_actionable_issue_opened(
            {"raw_payload": {"issue": {"title": "Help", "body": "cc @hannibal-hub-agents"}}}
        )
        is True
    )

    # Slash command
    assert (
        is_actionable_issue_opened(
            {"raw_payload": {"issue": {"title": "Triage request", "body": "/triage please"}}}
        )
        is True
    )

    # Untargeted bug report (should drop)
    assert (
        is_actionable_issue_opened(
            {"raw_payload": {"issue": {"title": "Bug in parser", "body": "Parser crashed on EOF"}}}
        )
        is False
    )


def test_filter_tier_0_duplicate_and_bot_events():
    """Verify filter_tier_0 drops duplicate deliveries and bot loops."""
    # Duplicate delivery
    drop, reason = filter_tier_0({"delivery_id": "del-123"}, processed_deliveries={"del-123": None})
    assert drop is True
    assert "duplicate" in reason

    # Bot sender
    drop, reason = filter_tier_0(
        {"delivery_id": "del-456", "sender": {"login": "hannibal-hub-agents[bot]"}}
    )
    assert drop is True
    assert "bot event" in reason

    # Push event
    drop, reason = filter_tier_0({"delivery_id": "del-789", "event_name": "push"})
    assert drop is True
    assert "push" in reason

    # Suppressed metadata action
    drop, reason = filter_tier_0(
        {
            "delivery_id": "del-999",
            "event_name": "pull_request",
            "action": "labeled",
            "sender": {"login": "developer"},
        }
    )
    assert drop is True
    assert "metadata" in reason


def test_tier1_router_base_branch_sync():
    """Verify handle_tier_1_programmatic terminates on base branch merge syncs."""
    gh = MagicMock()
    payload = {
        "repository": {"full_name": "owner/repo"},
        "canonical": "pull_request.synchronize",
        "raw_payload": {
            "pull_request": {
                "number": 101,
                "base": {"ref": "main"},
                "head": {"sha": "abc1234"},
            }
        },
    }
    with patch("webhook_agent.events.tier1_router.is_base_branch_merge_sync", return_value=True):
        status = handle_tier_1_programmatic(gh, payload, "pull_request.synchronize", "del-101")
        assert status == Tier1Status.TERMINAL


def test_tier1_router_ci_completed_dispatch():
    """Verify handle_tier_1_programmatic invokes CI completed handler on check_suite.completed."""
    gh = MagicMock()
    payload = {
        "repository": {"full_name": "owner/repo"},
        "raw_payload": {
            "repository": {"full_name": "owner/repo"},
            "check_suite": {"head_sha": "abc1234", "conclusion": "success"},
        },
    }
    mock_ci_handler = MagicMock()
    status = handle_tier_1_programmatic(
        gh,
        payload,
        "check_suite.completed",
        "del-202",
        handle_ci_completed_fn=mock_ci_handler,
    )
    assert status == Tier1Status.TERMINAL
    assert mock_ci_handler.called


def test_tier1_router_failing_ci_gate_terminates():
    """Verify handle_tier_1_programmatic terminates when CI is failing on a gated event."""
    gh = MagicMock()
    payload = {
        "repository": {"full_name": "owner/repo"},
        "canonical": "pull_request.opened",
        "raw_payload": {
            "pull_request": {
                "number": 102,
                "state": "open",
                "head": {"sha": "abc1234"},
            }
        },
    }
    with (
        patch("webhook_agent.events.tier1_router.is_ci_gate_enabled", return_value=True),
        patch("webhook_agent.events.tier1_router.evaluate_ci", return_value=CIState.FAIL),
    ):
        status = handle_tier_1_programmatic(gh, payload, "pull_request.opened", "del-303")
        assert status == Tier1Status.TERMINAL
