"""Unit tests for the Cloud Function webhook router (``router/main.py``).

The router is loaded **by path** rather than imported, for three reasons:

1. It is a standalone Cloud Function source dir, not part of the ``webhook_agent`` package,
   and deliberately has no ``__init__.py``.
2. Its module body validates required environment variables at import -- by design, so a
   misconfigured revision fails its startup probe instead of silently 401-ing forever. That
   means the import itself is a test target.
3. ``functions_framework`` is a router-only runtime dependency and is not installed in the
   dev environment, so it is stubbed.

Loading by path keeps this suite offline and leaves ``pyproject.toml`` untouched.
``tests/unit/review/test_replay_harness.py`` uses the same technique for a ``dev/`` script.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

_ROUTER_PATH = Path(__file__).resolve().parents[3] / "router" / "main.py"
_ISSUE_COMMENT_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "webhook_agent_creates_comment.json"
)

_TEST_SECRET = "test-webhook-secret"
_TEST_ENV = {
    "PUBSUB_PROJECT": "test-project",
    "PUBSUB_TOPIC": "test-topic",
    "WEBHOOK_SECRET": _TEST_SECRET,
}


def _load_router(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    """Execute ``router/main.py`` with a stubbed ``functions_framework`` and test env.

    Pass an override of ``None`` to leave that variable unset (for the cold-start tests).
    """
    stub = types.ModuleType("functions_framework")

    def _passthrough(fn: Any) -> Any:
        return fn

    stub.http = _passthrough
    monkeypatch.setitem(sys.modules, "functions_framework", stub)

    env = {**_TEST_ENV, **overrides}
    for key in _TEST_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is not None:
            monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("router_main", _ROUTER_PATH)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRequest:
    """Minimal stand-in for the ``functions_framework`` request object."""

    def __init__(self, body: bytes, headers: dict[str, str], method: str = "POST") -> None:
        self.method = method
        self.headers = headers
        self._body = body

    def get_data(self) -> bytes:
        return self._body


def _sign(body: bytes, secret: str = _TEST_SECRET) -> str:
    """Produce the ``X-Hub-Signature-256`` value GitHub would send for ``body``."""
    digest = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _request(body: bytes, event: str, **headers: str) -> _FakeRequest:
    base = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "test-delivery",
    }
    base.update(headers)
    return _FakeRequest(body, base)


def _post(module: Any, payload: Any, event: str, **headers: str) -> Any:
    body = json.dumps(payload).encode("utf-8")
    return module.github_webhook_router(_request(body, event, **headers))


def _installation_payload() -> dict[str, Any]:
    """Minimal stand-in for ``installation.new_permissions_accepted``.

    Top-level keys mirror a real delivery -- ``action``, ``installation``, ``sender`` -- and
    carry no repo-scoped object. That shape is what the body classifier keys on.
    """
    return {
        "action": "new_permissions_accepted",
        "installation": {"id": 150411146, "app_slug": "hannibal-hub-agents"},
        "sender": {"login": "cgj8702-org", "type": "Organization"},
    }


def _pr_payload() -> dict[str, Any]:
    """A repo-scoped payload: carries ``repository``, so it is not an installation event."""
    return {
        "action": "opened",
        "pull_request": {"number": 1},
        "repository": {"full_name": "cgj8702-org/hannibal-hub-agents"},
        "sender": {"login": "cgj8702", "type": "User"},
        "installation": {"id": 150411146},
    }


def _stub_publisher(module: Any, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the lazy Pub/Sub client with a mock and return it."""
    publisher = MagicMock()
    publisher.topic_path.return_value = "projects/test-project/topics/test-topic"
    publisher.publish.return_value = MagicMock()
    monkeypatch.setattr(module, "_get_publisher", lambda: publisher)
    return publisher


class TestHealthAndMethod:
    def test_get_returns_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        request = _FakeRequest(b"", {}, method="GET")
        assert module.github_webhook_router(request) == ({"status": "healthy"}, 200)

    def test_non_post_is_405(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        assert module.github_webhook_router(_FakeRequest(b"", {}, method="PUT"))[1] == 405


class TestColdStartValidation:
    @pytest.mark.parametrize("missing", ["PUBSUB_PROJECT", "PUBSUB_TOPIC", "WEBHOOK_SECRET"])
    def test_missing_env_fails_import(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        """A misconfigured revision must die at cold start, not 401 forever."""
        with pytest.raises(RuntimeError, match=missing):
            _load_router(monkeypatch, **{missing: None})


class TestSignatureVerification:
    def test_valid_signature_publishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        assert _post(module, _pr_payload(), "pull_request")[1] == 202
        assert publisher.publish.called

    def test_tampered_body_is_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = json.dumps(_pr_payload()).encode("utf-8")
        request = _FakeRequest(body, {"X-Hub-Signature-256": _sign(b"other")})
        assert module.github_webhook_router(request)[1] == 401

    def test_missing_signature_header_is_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = json.dumps(_pr_payload()).encode("utf-8")
        assert module.github_webhook_router(_FakeRequest(body, {}))[1] == 401

    def test_non_sha256_prefix_is_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = json.dumps(_pr_payload()).encode("utf-8")
        request = _FakeRequest(body, {"X-Hub-Signature-256": "sha1=deadbeef"})
        assert module.github_webhook_router(request)[1] == 401

    def test_signature_comparison_is_constant_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard against a 'simplification' to ==, which leaks via timing."""
        source = _ROUTER_PATH.read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source


class TestPing:
    def test_ping_returns_pong_without_publishing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        payload = {"zen": "Non-blocking is better than blocking.", "hook_id": 1}
        assert _post(module, payload, "ping") == ({"msg": "pong"}, 200)
        assert not publisher.publish.called


class TestPerimeterFilter:
    @pytest.mark.parametrize(
        "event",
        [
            "check_run",
            "status",
            "installation",
            "installation_repositories",
            "installation_target",
            "pull_request_review_thread",
            "workflow_run",
            "workflow_job",
        ],
    )
    def test_perimeter_filtered_events_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch, event: str
    ) -> None:
        """Cannot be unsubscribed (installation*) or dropped as defense-in-depth."""
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        response = _post(module, _installation_payload(), event)
        assert response[1] == 202
        assert "perimeter filter" in response[0]
        assert not publisher.publish.called

    def test_perimeter_filter_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        assert module.PERIMETER_FILTER_EVENTS == frozenset(
            {
                "check_run",
                "status",
                "installation",
                "installation_repositories",
                "installation_target",
                "pull_request_review_thread",
                "workflow_run",
                "workflow_job",
            }
        )


class TestBodyHeaderConsistency:
    def test_installation_body_with_wrong_header_is_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The body is signed and the header is not, so disagreement is rejected."""
        module = _load_router(monkeypatch)
        assert _post(module, _installation_payload(), "pull_request")[1] == 400

    def test_installation_header_mismatch_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        assert _post(module, _pr_payload(), "installation")[1] == 400

    def test_issue_comment_fixture_is_not_installation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: a real repo-scoped payload must still be published.

        The fixture carries ``installation`` *and* ``repository``/``issue``/``comment``, so a
        classifier that keyed on ``installation`` alone would silently drop it.
        """
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        payload = json.loads(_ISSUE_COMMENT_FIXTURE.read_text(encoding="utf-8"))
        assert _post(module, payload, "issue_comment")[1] == 202
        assert publisher.publish.called


class TestValidPublish:
    def test_valid_pr_event_is_published_with_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        payload = _pr_payload()
        assert _post(module, payload, "pull_request") == (
            "Event normalized and queued successfully",
            202,
        )
        assert publisher.publish.called

        call = publisher.publish.call_args
        data = json.loads(call.args[1])
        assert data["installation_id"] == 150411146
        assert data["delivery_id"] == "test-delivery"
        assert data["repository"]["full_name"] == "cgj8702-org/hannibal-hub-agents"

        kwargs = call.kwargs
        assert kwargs["event_name"] == "pull_request"
        assert kwargs["action"] == "opened"
        assert kwargs["repository"] == "cgj8702-org/hannibal-hub-agents"
        assert kwargs["installation_id"] == "150411146"


class TestErrorClassification:
    def test_non_utf8_body_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = b"\xff\xfe\x00bad"
        request = _FakeRequest(
            body,
            {
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
            },
        )
        assert module.github_webhook_router(request)[1] == 400

    def test_malformed_json_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = b"{not valid json"
        request = _FakeRequest(
            body,
            {
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
            },
        )
        assert module.github_webhook_router(request)[1] == 400

    def test_non_dict_json_root_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        body = json.dumps(["not", "an", "object"]).encode("utf-8")
        request = _FakeRequest(
            body,
            {
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
            },
        )
        assert module.github_webhook_router(request)[1] == 400

    def test_publish_failure_is_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load_router(monkeypatch)
        publisher = _stub_publisher(module, monkeypatch)
        publisher.publish.side_effect = Exception("Pub/Sub down")
        assert _post(module, _pr_payload(), "pull_request")[1] == 500


class TestCrossSystemParity:
    def test_router_drop_list_is_strict_subset_of_worker_suppression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The router may only drop events the worker would also suppress."""
        module = _load_router(monkeypatch)
        from webhook_agent.processor import SUPPRESSED_EVENT_NAMES

        router_set = module.PERIMETER_FILTER_EVENTS
        assert router_set.issubset(SUPPRESSED_EVENT_NAMES)
        assert router_set != SUPPRESSED_EVENT_NAMES  # strict subset


