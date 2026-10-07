"""Unit tests for bot identity detection and Jules agent coordination."""

from __future__ import annotations

from webhook_agent.core.agent_definition import CONVERSATIONAL_INSTRUCTION
from webhook_agent.github.bot_identity import _is_bot_event, _is_bot_sender, is_jules_sender
from webhook_agent.processor import WebhookProcessor


def test_is_jules_sender():
    assert is_jules_sender({"login": "google-labs-jules[bot]"}) is True
    assert is_jules_sender({"login": "google-labs-jules"}) is True
    assert is_jules_sender({"login": "google-jules[bot]"}) is True
    assert is_jules_sender({"login": "google-jules"}) is True
    assert is_jules_sender({"login": "custom-jules[bot]"}) is True
    # Plain 'jules' username represents an unrelated human user, not the bot
    assert is_jules_sender({"login": "jules"}) is False
    assert is_jules_sender({"login": "cgj8702"}) is False
    assert is_jules_sender({"login": "hannibal-hub-agents[bot]"}) is False
    assert is_jules_sender(None) is False
    assert is_jules_sender({}) is False


def test_is_bot_sender_exempts_jules():
    # Hannibal bot logins should be marked as bot sender
    assert _is_bot_sender({"login": "hannibal-hub-agents[bot]"}) is True
    assert _is_bot_sender({"login": "github-actions[bot]"}) is True

    # Jules must NOT be classified as this app's bot identity so its PRs can be audited
    assert _is_bot_sender({"login": "google-labs-jules[bot]"}) is False
    assert _is_bot_sender({"login": "google-jules[bot]"}) is False
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


def test_is_jules_sender_multi_signal():
    # Login match
    assert is_jules_sender({"login": "google-jules[bot]"}) is True
    assert is_jules_sender({"login": "custom-jules-agent[bot]"}) is True

    # Multi-signal match via performed_via_github_app
    generic_sender = {"login": "custom-bot[bot]"}
    payload_with_app = {"performed_via_github_app": {"slug": "google-jules"}}
    assert is_jules_sender(generic_sender, raw_payload=payload_with_app) is True

    comment_with_app = {"comment": {"performed_via_github_app": {"slug": "jules-preview-app"}}}
    assert is_jules_sender(generic_sender, raw_payload=comment_with_app) is True

    # Unrelated app should not match
    unrelated_payload = {"performed_via_github_app": {"slug": "codecov"}}
    assert is_jules_sender(generic_sender, raw_payload=unrelated_payload) is False


def test_processor_suppresses_jules_with_unregistered_login_via_app_slug():
    processor = WebhookProcessor()

    # Even if Jules changes its login handle to 'unregistered-worker[bot]',
    # performed_via_github_app catches it and prevents loop
    jules_with_app_comment = {
        "event_name": "issue_comment",
        "action": "created",
        "sender": {"login": "unregistered-worker[bot]"},
        "raw_payload": {
            "comment": {
                "user": {"login": "unregistered-worker[bot]"},
                "body": "Working on branch...",
                "performed_via_github_app": {"slug": "google-jules"},
            },
            "issue": {"number": 10},
        },
    }
    assert processor.should_process_event(jules_with_app_comment) is False


def test_conversational_instruction_has_jules_delegation_protocol():
    assert "@jules" not in CONVERSATIONAL_INSTRUCTION
    assert "google-labs-jules" in CONVERSATIONAL_INSTRUCTION
    assert "Jules Delegation Protocol" in CONVERSATIONAL_INSTRUCTION


def test_is_bot_sender_edge_cases():
    assert _is_bot_sender(None) is False
    assert _is_bot_sender("invalid") is False  # type: ignore[arg-type]
    assert _is_bot_sender({}) is False
    assert _is_bot_sender({"login": ""}) is False

    # Known logins
    assert _is_bot_sender({"login": "hannibal-hub-agents"}) is True
    assert _is_bot_sender({"login": "hannibal-hub-agents[bot]"}) is True
    assert _is_bot_sender({"login": "github-actions[bot]"}) is True

    # Suffix [bot] + substring
    assert _is_bot_sender({"login": "my-hannibal[bot]"}) is True
    assert _is_bot_sender({"login": "ci-agent[bot]"}) is True
    assert _is_bot_sender({"login": "unrelated[bot]"}) is False

    # Sender type Bot
    assert _is_bot_sender({"login": "hannibal-runner", "type": "Bot"}) is True
    assert _is_bot_sender({"login": "custom-agent-worker", "type": "Bot"}) is True
    assert _is_bot_sender({"login": "unrelated-runner", "type": "Bot"}) is False
    assert _is_bot_sender({"login": "hannibal-runner", "type": "User"}) is False


def test_is_bot_event_top_level_and_raw_sender():
    # Top-level sender
    assert _is_bot_event({"sender": {"login": "hannibal-hub-agents[bot]"}}) is True
    assert _is_bot_event({"sender": {"login": "human"}}) is False
    assert _is_bot_event({"sender": {"login": "human"}, "raw_payload": None}) is False

    # Raw payload sender
    assert (
        _is_bot_event(
            {
                "sender": {"login": "human"},
                "raw_payload": {"sender": {"login": "hannibal-hub-agents[bot]"}},
            }
        )
        is True
    )


def test_is_bot_event_comment_and_review_authors():
    # Comment author
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "comment": {"user": {"login": "hannibal-hub-agents[bot]"}},
                }
            }
        )
        is True
    )

    # Review author
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "review": {"user": {"login": "hannibal-hub-agents[bot]"}},
                }
            }
        )
        is True
    )

    # Review comment author
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "review_comment": {"user": {"login": "hannibal-hub-agents[bot]"}},
                }
            }
        )
        is True
    )


def test_is_bot_event_performed_via_github_app(monkeypatch):
    # Root level app slug
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "performed_via_github_app": {"slug": "hannibal-hub-agents"},
                }
            }
        )
        is True
    )

    # In comment
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "comment": {"performed_via_github_app": {"slug": "hannibal-hub-agents"}},
                }
            }
        )
        is True
    )

    # In review
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "review": {"performed_via_github_app": {"slug": "hannibal-hub-agents"}},
                }
            }
        )
        is True
    )

    # In review comment
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "review_comment": {"performed_via_github_app": {"slug": "hannibal-hub-agents"}},
                }
            }
        )
        is True
    )

    # App ID match via environment variable
    monkeypatch.setenv("GITHUB_APP_ID", "998877")
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "performed_via_github_app": {"id": 998877, "slug": "custom-app"},
                }
            }
        )
        is True
    )
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "performed_via_github_app": {"id": 112233, "slug": "custom-app"},
                }
            }
        )
        is False
    )

    # Unrelated app
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    assert (
        _is_bot_event(
            {
                "raw_payload": {
                    "performed_via_github_app": {"slug": "codecov"},
                }
            }
        )
        is False
    )

    # Clean non-bot event
    assert (
        _is_bot_event(
            {
                "sender": {"login": "octocat"},
                "raw_payload": {
                    "sender": {"login": "octocat"},
                    "comment": {"user": {"login": "octocat"}},
                },
            }
        )
        is False
    )
