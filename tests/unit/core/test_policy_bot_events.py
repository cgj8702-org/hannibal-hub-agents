"""Unit tests for partner-bot handling in ``resolve_event_policy``.

Covers the #441 exemption: partner bots may trigger ISSUE_TRIAGE, and nothing else.
"""

from __future__ import annotations

import pytest

from webhook_agent.core.policy import Mode, resolve_event_policy

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _event(canonical: str, login: str, comment_body: str = "", is_pr: bool = False) -> dict:
    """Build a minimally valid normalized event (session key derivation needs repo + number)."""
    issue_dict: dict = {"number": 441}
    if is_pr:
        issue_dict["pull_request"] = {"url": "https://api.github.com/repos/owner/repo/pulls/441"}
    raw: dict = {
        "sender": {"login": login},
        "issue": issue_dict,
    }
    if comment_body:
        raw["comment"] = {"body": comment_body, "user": {"login": login}}
    return {
        "canonical": canonical,
        "sender": {"login": login},
        "repository": {"full_name": "cgj8702-org/hannibal-hub-agents"},
        "raw_payload": raw,
    }


def test_partner_bot_issue_opened_is_triaged() -> None:
    policy = resolve_event_policy(_event("issues.opened", "cline-cloud[bot]"))
    assert policy.mode == Mode.ISSUE_TRIAGE


def test_partner_bot_issue_closed_is_triaged() -> None:
    policy = resolve_event_policy(_event("issues.closed", "cline-cloud[bot]"))
    assert policy.mode == Mode.ISSUE_TRIAGE


@pytest.mark.parametrize(
    "login",
    ["cline-cloud[bot]", "cline-cloud", "google-labs-jules[bot]"],
)
def test_partner_bot_issue_comment_without_intent_stays_ignored(login: str) -> None:
    """The exemption is issue events only; bot comments still need review intent."""
    policy = resolve_event_policy(_event("issue_comment.created", login, "thanks, looks good"))
    assert policy.mode == Mode.IGNORE


def test_partner_bot_comment_with_review_intent_is_audited() -> None:
    policy = resolve_event_policy(
        _event("issue_comment.created", "cline-cloud[bot]", "/re-review", is_pr=True)
    )
    assert policy.mode == Mode.FORMAL_AUDIT


def test_non_partner_bot_issue_opened_stays_ignored() -> None:
    policy = resolve_event_policy(_event("issues.opened", "some-random-bot[bot]"))
    assert policy.mode == Mode.IGNORE


def test_own_bot_issue_opened_stays_ignored() -> None:
    """Never triage our own filed issues: that would be a self-loop."""
    policy = resolve_event_policy(_event("issues.opened", "hannibal-hub-agents[bot]"))
    assert policy.mode == Mode.IGNORE


def test_human_issue_opened_is_triaged() -> None:
    event = _event("issues.opened", "cgj8702")
    event["raw_payload"]["issue"]["user"] = {"login": "cgj8702"}
    policy = resolve_event_policy(event)
    assert policy.mode == Mode.ISSUE_TRIAGE
