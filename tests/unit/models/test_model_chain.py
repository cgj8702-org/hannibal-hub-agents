"""Unit tests for model chain routing, tier capacity, and transient error detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from google.genai.errors import ServerError as GenAIServerError

from webhook_agent.models.model_chain import (
    _count_tokens_exact,
    _get_model_tpm_limit,
    _is_transient_error,
    _select_model_for_event,
    get_active_model,
    get_model_chain,
    is_gemini_3_plus,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_get_model_chain_free_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify free tier fallback chain defaults."""
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)

    chain = get_model_chain()
    assert chain[0] == "gemini-3.1-flash-lite"
    assert "gemini-3.5-flash-lite" in chain
    assert "gemini-3.8-flash" not in chain
    assert "gemini-3.7-flash" in chain
    assert "gemini-3.6-flash" in chain
    assert "gemini-3.5-flash" in chain
    assert "gemma-4-31b-it" in chain


def test_get_model_chain_free_tier_token_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Gemma is omitted for large payloads (>10k tokens) and kept for payloads <= 10k."""
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)

    large_payload_chain = get_model_chain(estimated_tokens=25000)
    assert "gemini-3.5-flash-lite" in large_payload_chain
    assert "gemini-3.8-flash" not in large_payload_chain
    assert "gemma-4-31b-it" not in large_payload_chain
    assert "gemma-4-26b-a4b-it" not in large_payload_chain

    # Boundary check: just over 10k
    boundary_over = get_model_chain(estimated_tokens=10001)
    assert "gemma-4-31b-it" not in boundary_over
    assert "gemma-4-26b-a4b-it" not in boundary_over

    # Boundary check: exactly 10k
    boundary_at = get_model_chain(estimated_tokens=10000)
    assert "gemma-4-31b-it" in boundary_at
    assert "gemma-4-26b-a4b-it" in boundary_at

    small_payload_chain = get_model_chain(estimated_tokens=5000)
    assert "gemini-3.5-flash-lite" in small_payload_chain
    assert "gemma-4-31b-it" in small_payload_chain
    assert "gemma-4-26b-a4b-it" in small_payload_chain


def test_get_model_chain_paid_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify paid tier fallback chain defaults."""
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)

    chain = get_model_chain()
    assert chain[0] == "gemini-3.1-flash-lite"
    assert "gemini-3.5-flash-lite" in chain
    assert "gemini-3.5-flash" in chain
    assert "gemini-3.6-flash" in chain
    assert "gemini-3.7-flash" in chain


def test_get_model_chain_custom_primary_with_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify PRIMARY_MODEL with 'models/' prefix is stripped and placed first."""
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.setenv("PRIMARY_MODEL", "models/custom-experimental-model")

    chain = get_model_chain()
    assert chain[0] == "custom-experimental-model"


def test_get_model_chain_all_depleted_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify when registry filters all models, fallback to raw deduped chain."""
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    with patch(
        "webhook_agent.models.model_chain._DEPLETED_MODEL_REGISTRY.filter_chain", return_value=[]
    ):
        chain = get_model_chain()
        assert len(chain) > 0
        assert chain[0] == "gemini-3.1-flash-lite"


def test_select_model_for_event_dynamic_routing_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify dynamic routing disabled returns primary model directly."""
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "0")
    monkeypatch.setenv("PRIMARY_MODEL", "primary-test-model")

    event = {"canonical": "issue_comment.created", "raw_payload": {}}
    # Since dynamic routing is disabled, it should return the primary_model override
    # But wait, if the code is: primary = os.environ.get("PRIMARY_MODEL", default_primary)
    # and we set PRIMARY_MODEL = "primary-test-model", it should return that.
    # The failure 'gemini-3.5-flash-lite' == 'primary-test-model' suggests
    # that _select_model_for_event is ignoring the PRIMARY_MODEL env var
    # when routing is disabled? No, let's check the code.
    assert _select_model_for_event(event) == "primary-test-model"


def test_select_model_for_event_pull_request_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify pull request opened/synchronized routes to primary model."""
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.setenv("PRIMARY_MODEL", "primary-pr-model")

    event_opened = {"canonical": "pull_request.opened", "raw_payload": {}}
    assert _select_model_for_event(event_opened) == "primary-pr-model"

    event_sync = {"canonical": "pull_request.synchronize", "raw_payload": {}}
    assert _select_model_for_event(event_sync) == "primary-pr-model"


def test_select_model_for_event_comment_with_review_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify comment events with review intent keywords or bot mention route to primary."""
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.setenv("PRIMARY_MODEL", "primary-comment-model")
    monkeypatch.setenv("LIGHTWEIGHT_MODEL", "lightweight-comment-model")

    # Bot mention
    mention_event = {
        "canonical": "issue_comment.created",
        "raw_payload": {"comment": {"body": "Hey @hannibal-hub-agents can you check this?"}},
    }
    assert _select_model_for_event(mention_event) == "primary-comment-model"

    # Review keyword (/review)
    review_event = {
        "canonical": "pull_request_review_comment.created",
        "raw_payload": {"comment": {"body": "Please /review the changes in this block"}},
    }
    assert _select_model_for_event(review_event) == "primary-comment-model"

    # Routine comment without keyword
    routine_event = {
        "canonical": "issue_comment.created",
        "raw_payload": {"comment": {"body": "Just saying thanks!"}},
    }
    assert _select_model_for_event(routine_event) == "lightweight-comment-model"


def test_select_model_for_event_routine_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify routine non-PR events route to lightweight model."""
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.setenv("LIGHTWEIGHT_MODEL", "lightweight-routine-model")

    event = {"canonical": "push", "raw_payload": {}}
    assert _select_model_for_event(event) == "lightweight-routine-model"


def test_select_model_for_event_depleted_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that if target model is depleted, it falls back to first available in chain."""
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.setenv("PRIMARY_MODEL", "depleted-model")

    with patch(
        "webhook_agent.models.model_chain._DEPLETED_MODEL_REGISTRY.is_depleted",
        side_effect=lambda m: m == "depleted-model",
    ):
        with patch(
            "webhook_agent.models.model_chain.get_model_chain", return_value=["fallback-model"]
        ):
            event = {"canonical": "pull_request.opened", "raw_payload": {}}
            assert _select_model_for_event(event) == "fallback-model"


def test_get_active_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_active_model delegates to event selection or chain head."""
    # With event data
    event = {"canonical": "pull_request.opened", "raw_payload": {}}
    with patch(
        "webhook_agent.models.model_chain._select_model_for_event", return_value="event-model"
    ):
        assert get_active_model(event) == "event-model"

    # Without event data, chain available
    with patch(
        "webhook_agent.models.model_chain.get_model_chain",
        return_value=["head-model", "second-model"],
    ):
        assert get_active_model() == "head-model"

    # Without event data, empty chain, free tier fallback
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    with patch("webhook_agent.models.model_chain.get_model_chain", return_value=[]):
        assert get_active_model() == "gemini-3.5-flash-lite"


def test_get_model_tpm_limit() -> None:
    """Verify _get_model_tpm_limit reads from registry or returns safe defaults."""
    # Real read from gemini_models.json for known model
    tpm = _get_model_tpm_limit(model="gemini-3.5-flash-lite", tier="free")
    assert isinstance(tpm, int)
    assert tpm > 0

    # Unknown model with exception/fallback
    tpm_free_fallback = _get_model_tpm_limit(model="nonexistent-model-xyz", tier="free")
    assert tpm_free_fallback in (15000, 250000)

    tpm_paid_fallback = _get_model_tpm_limit(model="nonexistent-model-xyz", tier="paid")
    assert tpm_paid_fallback in (100000, 4000000)


def test_count_tokens_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _count_tokens_exact handles empty text, API calls, and fallbacks."""
    assert _count_tokens_exact("") == 0

    # No API key -> heuristic len(text) // 4
    with patch("webhook_agent.models.model_chain.get_active_api_key", return_value=None):
        assert _count_tokens_exact("12345678") == 2

    # With API key and successful Client call
    mock_resp = MagicMock()
    mock_resp.total_tokens = 42
    mock_client = MagicMock()
    mock_client.models.count_tokens.return_value = mock_resp

    with patch("webhook_agent.models.model_chain.get_active_api_key", return_value="test-key"):
        with patch("google.genai.Client", return_value=mock_client):
            assert _count_tokens_exact("test text", model="gemini-3.5-flash-lite") == 42

    # With API call failure -> fallback
    with patch("webhook_agent.models.model_chain.get_active_api_key", return_value="test-key"):
        with patch("google.genai.Client", side_effect=RuntimeError("API failure")):
            assert _count_tokens_exact("12345678") == 2


def test_is_transient_error() -> None:
    """Verify _is_transient_error detects retryable codes and error strings."""
    # GenAIServerError with transient codes
    err503 = GenAIServerError(503, {"error": "Service Unavailable"})
    assert _is_transient_error(err503) is True

    err429 = GenAIServerError(429, {"error": "Rate Limit"})
    assert _is_transient_error(err429) is True

    err400 = GenAIServerError(400, {"error": "Bad Request"})
    assert _is_transient_error(err400) is False

    # Exception strings
    assert _is_transient_error(Exception("HTTP 503 service temporarily unavailable")) is True
    assert _is_transient_error(Exception("Resource_Exhausted quota exceeded")) is True
    assert _is_transient_error(Exception("High demand on servers")) is True
    assert _is_transient_error(Exception("429 Too Many Requests")) is True

    # Custom exception classes
    class ResourceExhaustedError(Exception):
        pass

    assert _is_transient_error(ResourceExhaustedError("Quota")) is True

    # Non-transient errors
    assert _is_transient_error(ValueError("Invalid syntax")) is False
    assert _is_transient_error(KeyError("missing_key")) is False


def test_is_gemini_3_plus() -> None:
    """Verify is_gemini_3_plus distinguishes Gemini 3+ from Gemma and older models."""
    assert is_gemini_3_plus(None) is False
    assert is_gemini_3_plus("") is False
    assert is_gemini_3_plus("gemma-4-31b-it") is False
    assert is_gemini_3_plus("models/gemma-4-26b-a4b-it") is False

    assert is_gemini_3_plus("gemini-3.5-flash-lite") is True
    assert is_gemini_3_plus("gemini-3.8-flash") is True
    assert is_gemini_3_plus("models/gemini-3.1-flash-lite") is True

    assert is_gemini_3_plus("gemini-2.5-flash") is False
    assert is_gemini_3_plus("gemini-2.5-flash-lite") is False
    assert is_gemini_3_plus("gemini-invalid-version") is False


def test_select_model_for_event_deep_reasoning_escalation_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify comment with deep reasoning directive escalates to gemini-2.5-pro on paid tier."""
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("DEEP_REASONING_MODEL", raising=False)

    event = {
        "canonical": "issue_comment.created",
        "raw_payload": {"comment": {"body": "Please perform a /deep audit of the AST changes."}},
    }
    assert _select_model_for_event(event) == "gemini-2.5-pro"


def test_select_model_for_event_heavy_diff_payload_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify massive PR diff payloads (>50k tokens) route to gemini-3.8-flash on paid tier."""
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.setenv("ENABLE_DYNAMIC_MODEL_ROUTING", "1")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)

    huge_diff = "diff --git a/test.py b/test.py\n+line\n" * 25000  # ~100k+ tokens
    event = {
        "canonical": "pull_request.opened",
        "raw_payload": {"pr_diff": huge_diff},
    }
    assert _select_model_for_event(event) == "gemini-3.8-flash"


def test_get_model_chain_heavy_payload_paid_tpm_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify heavy payload on paid tier guarantees 4M TPM models lead the fallback chain."""
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    monkeypatch.delenv("PRIMARY_MODEL", raising=False)

    chain = get_model_chain(estimated_tokens=80000)
    # The first 3 models must be the 4M TPM leaders
    assert chain[0] in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash")
    assert chain[1] in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash")
    # 3.6-flash is one of the 4M TPM leaders in position 2 or 3
    assert chain[2] in (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    )
    # 3.5-flash is 4th in the chain (after the 3 4M TPM leaders)
    assert "gemini-3.5-flash" not in chain[:3]
    # gemini-3.6-flash is in the paid-tier heavy-payload chain
    assert "gemini-3.6-flash" in chain
