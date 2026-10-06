"""Unit tests for bot identity detection and Jules agent coordination."""

from __future__ import annotations

from webhook_agent.bot_identity import _is_bot_sender, is_jules_sender
from webhook_agent.core.agent_definition import CONVERSATIONAL_INSTRUCTION
from webhook_agent.processor import WebhookProcessor


def test_is_jules_sender():
    assert is_jules_sender({"login": "google-jules[bot]"}) is True
    assert is_jules_sender({"login": "jules[bot]"}) is True
    assert is_jules_sender({"login": "google-jules"}) is True
    assert is_jules_sender({"login": "jules"}) is True
    assert is_jules_sender({"login": "cgj8702"}) is False
    assert is_jules_sender({"login": "hannibal-hub-agents[bot]"}) is False
    assert is_jules_sender(None) is False
    assert is_jules_sender({}) is False


def test_is_bot_sender_exempts_jules():
    # Hannibal bot logins should be marked as bot sender
    assert _is_bot_sender({"login": "hannibal-hub-agents[bot]"}) is True
    assert _is_bot_sender({"login": "github-actions[bot]"}) is True

    # Jules must NOT be classified as this app's bot identity so its PRs can be audited
    assert _is_bot_sender({"login": "google-jules[bot]"}) is False
    assert _is_bot_sender({"login": "jules[bot]"}) is False
    assert _is_bot_sender({"login": "cgj8702"}) is False


def test_processor_processes_jules_pr_events():
    processor = WebhookProcessor()

    jules_pr_opened = {
        "event_name": "pull_request",
        "action": "opened",
        "sender": {"login": "google-jules[bot]"},
        "raw_payload": {
            "pull_request": {"number": 123, "state": "open"},
            "repository": {"full_name": "cgj8702-org/test-repo"},
        },
    }
    assert processor.should_process_event(jules_pr_opened) is True

    jules_pr_sync = {
        "event_name": "pull_request",
        "action": "synchronize",
        "sender": {"login": "jules[bot]"},
        "raw_payload": {
            "pull_request": {"number": 123, "state": "open"},
            "repository": {"full_name": "cgj8702-org/test-repo"},
        },
    }
    assert processor.should_process_event(jules_pr_sync) is True


def test_processor_suppresses_jules_conversational_comments_without_review_intent():
    processor = WebhookProcessor()

    # General status comment from Jules without review intent or mention
    jules_comment = {
        "event_name": "issue_comment",
        "action": "created",
        "sender": {"login": "google-jules[bot]"},
        "raw_payload": {
            "comment": {
                "user": {"login": "google-jules[bot]"},
                "body": "I have created branch `jules/fix-bug` and am running tests.",
            },
            "issue": {"number": 42},
        },
    }
    assert processor.should_process_event(jules_comment) is False


def test_processor_allows_jules_comments_with_review_intent_or_mention():
    processor = WebhookProcessor()

    # Comment with review intent
    jules_review_comment = {
        "event_name": "issue_comment",
        "action": "created",
        "sender": {"login": "google-jules[bot]"},
        "raw_payload": {
            "comment": {
                "user": {"login": "google-jules[bot]"},
                "body": "Work complete, please review PR #42",
            },
            "issue": {"number": 42, "pull_request": {"url": "http://example.com"}},
        },
    }
    assert processor.should_process_event(jules_review_comment) is True

    # Comment explicitly mentioning Hannibal
    jules_mention_comment = {
        "event_name": "issue_comment",
        "action": "created",
        "sender": {"login": "google-jules[bot]"},
        "raw_payload": {
            "comment": {
                "user": {"login": "google-jules[bot]"},
                "body": "@hannibal-hub-agents can you check this edge case?",
            },
            "issue": {"number": 42},
        },
    }
    assert processor.should_process_event(jules_mention_comment) is True


def test_conversational_instruction_has_jules_delegation_protocol():
    assert "@jules" in CONVERSATIONAL_INSTRUCTION
    assert "Jules Delegation Protocol" in CONVERSATIONAL_INSTRUCTION
