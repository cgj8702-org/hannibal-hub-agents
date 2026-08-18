"""Unit tests for centralized constants and Secret Manager secret resolution."""

from unittest.mock import MagicMock, patch

import pytest

from logic.constants import (
    DEFAULT_FEATURE_AGENT_PROJECT,
    DEFAULT_GITHUB_APP_ID,
    DEFAULT_GITHUB_INSTALLATION_ID,
    DEFAULT_GITHUB_REPOSITORY,
    DEFAULT_PUBSUB_PROJECT,
    DEFAULT_WEBHOOK_FREE_PROJECT,
    DEFAULT_WEBHOOK_PAID_PROJECT,
)
from logic.secret_manager import resolve_secret

pytestmark = [pytest.mark.unit]


def test_constants_defaults() -> None:
    assert DEFAULT_GITHUB_APP_ID == "4133145"
    assert DEFAULT_GITHUB_INSTALLATION_ID == "150411146"
    assert DEFAULT_GITHUB_REPOSITORY == "cgj8702-org/hannibal-hub-agents"
    assert DEFAULT_PUBSUB_PROJECT == "cgj8702-webhook-agent"
    assert DEFAULT_WEBHOOK_PAID_PROJECT == "cgj8702-webhook-agent"
    assert DEFAULT_WEBHOOK_FREE_PROJECT == "gen-lang-client-0615466973"
    assert DEFAULT_FEATURE_AGENT_PROJECT == "gen-lang-client-0613181237"


def test_resolve_secret_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_FREE_KEY", "env-secret-key-123")
    key = resolve_secret("FEATURE_AGENT_FREE_KEY")
    assert key == "env-secret-key-123"


def test_resolve_secret_fallback_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_FREE_KEY", raising=False)

    mock_sm = MagicMock()
    mock_payload = MagicMock()
    mock_payload.payload.data.decode.return_value = "secret-manager-key-456"
    mock_sm.SecretManagerServiceClient.return_value.access_secret_version.return_value = mock_payload

    with patch.dict("sys.modules", {"google.cloud.secretmanager": mock_sm}):
        with patch.dict("logic.secret_manager._SECRET_CACHE", {}, clear=True):
            val = resolve_secret("WEBHOOK_FREE_KEY")
            assert val == "secret-manager-key-456"
