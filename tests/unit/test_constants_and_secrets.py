"""Unit tests for centralized constants and Secret Manager secret resolution."""

import pytest

from webhook_agent.constants import (
    DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS,
    DEFAULT_ENABLE_JULES_PR_PROXY,
    DEFAULT_GITHUB_APP_ID,
    DEFAULT_GITHUB_INSTALLATION_ID,
    DEFAULT_GITHUB_REPOSITORY,
    DEFAULT_PUBSUB_PROJECT,
    DEFAULT_WEBHOOK_FREE_PROJECT,
    DEFAULT_WEBHOOK_PAID_PROJECT,
)
from webhook_agent.models.secret_manager import resolve_secret

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_constants_defaults() -> None:
    assert DEFAULT_GITHUB_APP_ID == "4133145"
    assert DEFAULT_GITHUB_INSTALLATION_ID == "150411146"
    assert DEFAULT_GITHUB_REPOSITORY == "cgj8702-org/hannibal-hub-agents"
    assert DEFAULT_PUBSUB_PROJECT == "cgj8702-webhook-agent"
    assert DEFAULT_WEBHOOK_PAID_PROJECT == "cgj8702-webhook-agent"
    assert DEFAULT_WEBHOOK_FREE_PROJECT == "gen-lang-client-0615466973"
    assert DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS == "1"
    assert DEFAULT_ENABLE_JULES_PR_PROXY == "0"


def test_resolve_secret_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_FREE_KEY", "resolved_from_environment")
    key = resolve_secret("WEBHOOK_FREE_KEY")
    assert key == "resolved_from_environment"


def test_resolve_secret_unset_or_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_FREE_KEY", raising=False)
    assert resolve_secret("WEBHOOK_FREE_KEY", default="fallback_val") == "fallback_val"

    monkeypatch.setenv("WEBHOOK_FREE_KEY", "dummy")
    assert resolve_secret("WEBHOOK_FREE_KEY", default="fallback_val") == "fallback_val"

    monkeypatch.setenv("WEBHOOK_FREE_KEY", "dummy-key-for-dev")
    assert resolve_secret("WEBHOOK_FREE_KEY") == ""

    monkeypatch.setenv("WEBHOOK_FREE_KEY", "none")
    assert resolve_secret("WEBHOOK_FREE_KEY") == ""
