"""FastAPI webhook receiver — normalizes incoming GitHub webhook payloads.

Every delivery is parsed, normalized to a consistent shape, and published to
Pub/Sub for downstream processing by the worker.

Normalized event structure:
{
    "delivery_id": str,
    "event_name": str,         # X-GitHub-Event header value
    "action": str | None,      # payload.action field if present
    "sender": dict | None,     # payload.sender object
    "installation": dict | None,  # payload.installation object
    "repository": dict | None,    # payload.repository object
    "raw_payload": dict,       # the full parsed JSON body
}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from os import environ
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .enqueue import publish_webhook_message

logger = logging.getLogger("webhook_receiver")

app = FastAPI(title="hannibal-hub-webhook-agent")


def verify_github_signature(secret: bytes, body: bytes, signature_header: str) -> bool:
    """Verify GitHub HMAC hex signature (sha256)."""
    if not signature_header:
        return False
    try:
        alg, sig = signature_header.split("=", 1)
    except ValueError:
        return False
    if alg not in ("sha1", "sha256"):
        return False
    if alg == "sha1":
        digest = hmac.new(secret, body, hashlib.sha1).hexdigest()
    else:
        digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig)


def normalize_payload(
    event_name: str,
    raw_payload: dict[str, Any],
    delivery_id: str,
) -> dict[str, Any]:
    """Normalize incoming webhook payload into a consistent event envelope.

    Every downstream consumer (worker, agent core) should rely on the top-level
    keys documented in this module rather than crawling the raw payload directly.
    """
    return {
        "delivery_id": delivery_id,
        "event_name": event_name,
        "action": raw_payload.get("action"),
        "sender": raw_payload.get("sender"),
        "installation": raw_payload.get("installation"),
        "repository": raw_payload.get("repository"),
        "raw_payload": raw_payload,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> Response:
    """Receive and normalize incoming GitHub webhook deliveries.

    Steps:
    1. Read raw body
    2. Verify signature using ``WEBHOOK_SECRET`` env var (optional)
    3. Parse JSON body and normalize into a consistent event envelope
    4. Publish the normalized event to Pub/Sub for downstream workers
    """
    body = await request.body()
    secret = os.environ.get("WEBHOOK_SECRET")
    if secret:
        ok = verify_github_signature(secret.encode(), body, x_hub_signature_256 or "")
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid signature")

    delivery_id = x_github_delivery or "unknown"
    event_name = x_github_event or "unknown"

    # Parse the raw JSON body
    try:
        raw_payload: dict[str, Any] = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("failed to parse webhook body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Normalize the event for downstream consumers
    normalized = normalize_payload(
        event_name=event_name,
        raw_payload=raw_payload,
        delivery_id=delivery_id,
    )

    logger.info(
        "received event=%s action=%s delivery=%s sender=%s repo=%s",
        normalized["event_name"],
        normalized["action"],
        delivery_id,
        normalized["sender"].get("login") if normalized["sender"] else None,
        normalized["repository"].get("full_name") if normalized["repository"] else None,
    )

    # Publish to Pub/Sub
    try:
        topic = environ.get("PUBSUB_TOPIC")
        if topic:
            publish_webhook_message(
                topic,
                payload=normalized,
                attributes={
                    "delivery_id": delivery_id,
                    "event_name": event_name,
                    "action": str(normalized["action"] or ""),
                },
            )
            logger.info(
                "enqueued delivery=%s event=%s to topic=%s",
                delivery_id,
                event_name,
                topic,
            )
        else:
            logger.warning(
                "PUBSUB_TOPIC not set; skipping enqueue for delivery=%s", delivery_id
            )
    except Exception:
        logger.exception("failed to enqueue delivery=%s", delivery_id)

    return Response(status_code=202)
