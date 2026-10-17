"""GitHub webhook ingress: verify, filter, normalize, and publish to Pub/Sub.

Deployed as a Cloud Function (source upload + ``requirements.txt``), entry point
``github_webhook_router``. This is the trust boundary: the HMAC signature is verified over
the raw body before anything else touches it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

import functions_framework
from google.cloud import pubsub_v1

logger = logging.getLogger(__name__)

# Events dropped at the perimeter before they ever cost a Pub/Sub round trip.
#
# The first three cannot be unsubscribed (GitHub delivers them to every App);
# the rest we *could* unsubscribe, but drop here as defense-in-depth so a
# re-subscription can't silently reintroduce them. Kept as a named constant so
# tests can assert it stays a strict subset of the worker's suppression list
# (see src/webhook_agent/processor.py).
PERIMETER_FILTER_EVENTS: frozenset[str] = frozenset(
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

# Top-level keys that appear only on repo-scoped deliveries. Every installation lifecycle
# payload carries "installation" *and* none of these; every repo event carries "repository".
# This is what lets us classify from the signed body instead of the unsigned header.
_REPO_SCOPED_KEYS: frozenset[str] = frozenset(
    {
        "repository",
        "issue",
        "pull_request",
        "comment",
        "review",
    }
)


def _get_required_env(name: str) -> str:
    """Return a required environment variable, or fail the cold start loudly.

    Validating at import makes a misconfigured revision fail its startup probe rather than
    silently rejecting every webhook with 401 forever.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PUBSUB_PROJECT = _get_required_env("PUBSUB_PROJECT")
PUBSUB_TOPIC = _get_required_env("PUBSUB_TOPIC")
WEBHOOK_SECRET = _get_required_env("WEBHOOK_SECRET")

_publisher: pubsub_v1.PublisherClient | None = None


def _get_publisher() -> pubsub_v1.PublisherClient:
    """Return the shared Pub/Sub client, constructing it on first use.

    Constructing at import resolves credentials at import, which makes the module
    unimportable without GCP credentials (so it cannot be unit tested) and turns a
    credential problem into an import-time crash. One client is still reused across warm
    invocations.
    """
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verify GitHub's HMAC-SHA256 signature over the raw request body."""
    if not signature_header:
        return False
    if not WEBHOOK_SECRET:
        return False
    try:
        sha_name, signature = signature_header.split("=")
    except ValueError:
        return False
    if sha_name != "sha256":
        return False

    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature)


def _body_event_class(body: dict[str, Any]) -> str:
    """Classify a delivery from its *signed* body.

    Returns ``"ping"``, ``"installation"``, or ``"unknown"``. The body cannot yield the full
    event name -- GitHub puts that in the unsigned ``X-GitHub-Event`` header, and several
    payloads share top-level keys -- but it can decide the two cases this router acts on.
    """
    # ping is the only payload carrying `zen`, and the only one with no `action`.
    if "zen" in body and "action" not in body:
        return "ping"
    # Installation lifecycle deliveries are the only ones carrying `installation` without a
    # repo-scoped object. `installation` alone is not a discriminator: every App delivery
    # includes it.
    if "installation" in body and _REPO_SCOPED_KEYS.isdisjoint(body):
        return "installation"
    return "unknown"


def normalize_payload(
    event_name: str,
    raw_payload: dict[str, Any],
    delivery_id: str,
) -> dict[str, Any]:
    """Normalize a delivery into the envelope the worker consumes.

    ``installation_id`` is flattened to a top-level int because the worker reads
    ``event_data["installation_id"]``; sending only the nested ``installation`` object left
    that key permanently ``None``.
    """
    installation = raw_payload.get("installation")
    return {
        "delivery_id": delivery_id,
        "event_name": event_name,
        "action": raw_payload.get("action"),
        "sender": raw_payload.get("sender"),
        "installation": installation,
        "installation_id": installation.get("id") if isinstance(installation, dict) else None,
        "repository": raw_payload.get("repository"),
        "raw_payload": raw_payload,
    }


def _attribute(value: Any) -> str:
    """Render a Pub/Sub attribute.

    Attribute values must be non-empty strings, and ``None`` must not become the literal
    ``"None"``.
    """
    return "" if value is None else str(value)


def _publish(normalized: dict[str, Any], delivery_id: str, event_type: str) -> None:
    """Publish the normalized envelope, with a bounded wait."""
    publisher = _get_publisher()
    if PUBSUB_TOPIC.startswith("projects/"):
        topic = PUBSUB_TOPIC
    else:
        topic = publisher.topic_path(PUBSUB_PROJECT, PUBSUB_TOPIC)

    repository = normalized.get("repository")
    repository_full_name = repository.get("full_name", "") if isinstance(repository, dict) else ""

    future = publisher.publish(
        topic,
        json.dumps(normalized).encode("utf-8"),
        delivery_id=delivery_id,
        event_name=event_type,
        action=_attribute(normalized.get("action")),
        repository=_attribute(repository_full_name),
        installation_id=_attribute(normalized.get("installation_id")),
    )
    # A client-side timeout does not cancel an in-flight publish. If it lands anyway, the
    # worker's delivery_id dedup cache absorbs GitHub's retry.
    future.result(timeout=5.0)


@functions_framework.http
def github_webhook_router(request: Any) -> tuple[str | dict[str, Any], int]:
    """Cloud Function entry point."""
    if request.method == "GET":
        return {"status": "healthy"}, 200
    if request.method != "POST":
        return "Method Not Allowed", 405

    payload_body = request.get_data()
    signature_header = request.headers.get("X-Hub-Signature-256") or ""
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")

    # 1. Verify the signature over the raw bytes before anything else touches them.
    if not verify_signature(payload_body, signature_header):
        return "Unauthorized: Signature mismatch", 401

    # 2. Strict decode + parse. A malformed body is the caller's fault, so it is a 400:
    #    a 5xx would ask GitHub to redeliver a request that can never succeed.
    try:
        body = json.loads(payload_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Bad Request: malformed JSON body", 400
    if not isinstance(body, dict):
        return "Bad Request: payload root must be an object", 400

    # 3. The body is signed; the header is not. Decide from the body, and reject a header
    #    that positively disagrees rather than trusting it.
    body_class = _body_event_class(body)

    if body_class == "ping":
        return {"msg": "pong"}, 200

    installation_events = {
        "installation",
        "installation_repositories",
        "installation_target",
    }

    if event_type in installation_events and not _REPO_SCOPED_KEYS.isdisjoint(body):
        # Header claims an installation event, but the body carries repo-scoped keys.
        return "Bad Request: event type does not match payload", 400

    if (
        body_class == "installation"
        and event_type not in PERIMETER_FILTER_EVENTS
        and event_type != "unknown"
    ):
        # Body is an installation payload, but header claims an unrelated non-perimeter event.
        return "Bad Request: event type does not match payload", 400

    if event_type in PERIMETER_FILTER_EVENTS or body_class == "installation":
        return "Event acknowledged and dropped by perimeter filter", 202

    # 4. Publish.
    normalized = normalize_payload(event_type, body, delivery_id)
    try:
        _publish(normalized, delivery_id, event_type)
    except Exception:
        logger.exception("Publish failed (delivery=%s event=%s)", delivery_id, event_type)
        return "Internal Server Error", 500

    return "Event normalized and queued successfully", 202
