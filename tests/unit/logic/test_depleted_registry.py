"""Unit tests for depletion classification in Firestore and Simple DepletedModelRegistry."""

import pytest

from webhook_agent.logic.firestore_registry import FirestoreDepletedModelRegistry
from webhook_agent.logic.model_chain import DepletedModelRegistry

pytestmark = [pytest.mark.unit]


class Fake503Error(Exception):
    def __init__(self, message: str = "This model is currently experiencing high demand.") -> None:
        super().__init__(f"503 UNAVAILABLE. {message}")


class FakeWrapperError(Exception):
    pass


def test_firestore_registry_classifies_503_high_demand() -> None:
    registry = FirestoreDepletedModelRegistry()
    registry._db = None  # in-memory mode

    err = Fake503Error()
    registry.mark_depleted("gemini-3.5-flash-lite", error=err)

    assert "gemini-3.5-flash-lite" in registry._local_depleted
    _, cooldown = registry._local_depleted["gemini-3.5-flash-lite"]
    assert cooldown == 120.0


def test_firestore_registry_unwraps_nested_503_exception() -> None:
    registry = FirestoreDepletedModelRegistry()
    registry._db = None

    cause = Fake503Error()
    wrapped = FakeWrapperError("Dynamic node webhook_agent failed")
    wrapped.__cause__ = cause

    registry.mark_depleted("gemma-4-31b-it", error=wrapped)

    assert "gemma-4-31b-it" in registry._local_depleted
    _, cooldown = registry._local_depleted["gemma-4-31b-it"]
    assert cooldown == 120.0


def test_firestore_registry_classifies_generic_429() -> None:
    registry = FirestoreDepletedModelRegistry()
    registry._db = None

    err = Exception("429 RESOURCE_EXHAUSTED: quota limit reached")
    registry.mark_depleted("gemini-3.5-flash-lite", error=err)

    _, cooldown = registry._local_depleted["gemini-3.5-flash-lite"]
    assert cooldown == 60.0


def test_firestore_registry_classifies_rpd() -> None:
    registry = FirestoreDepletedModelRegistry()
    registry._db = None

    err = Exception("Quota exceeded: RequestsPerDay exceeded")
    registry.mark_depleted("gemini-2.5-flash", error=err)

    _, cooldown = registry._local_depleted["gemini-2.5-flash"]
    assert cooldown == 86400.0


def test_simple_registry_classifies_nested_503() -> None:
    registry = DepletedModelRegistry()

    cause = Fake503Error()
    wrapped = FakeWrapperError("Node execution failed")
    wrapped.__context__ = cause

    registry.mark_depleted("gemma-4-26b-a4b-it", error=wrapped)

    assert "gemma-4-26b-a4b-it" in registry._depleted
    _, cooldown = registry._depleted["gemma-4-26b-a4b-it"]
    assert cooldown == 120.0


def test_firestore_registry_classifies_tpm_with_retry_delay() -> None:
    from google.genai.errors import ClientError

    registry = FirestoreDepletedModelRegistry()
    registry._db = None

    tpm_json = {
        "error": {
            "code": 429,
            "message": "Quota exceeded for input tokens",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "54.5s",
                },
            ],
        }
    }
    err = ClientError(code=429, response_json=tpm_json)
    registry.mark_depleted("gemini-3.5-flash-lite", error=err)

    _, cooldown = registry._local_depleted["gemini-3.5-flash-lite"]
    assert cooldown == 54.5
