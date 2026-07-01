import hashlib
import hmac
import logging
import os
from os import environ

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .enqueue import publish_webhook_message

logger = logging.getLogger("webhook_receiver")

app = FastAPI(title="github-webhook-agent-starter")


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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> Response:
    """Minimal webhook receiver:

    - reads raw body
    - verifies signature using `WEBHOOK_SECRET` env var (if present)
    - enqueues a job (stubbed)
    - returns 202 Accepted
    """
    body = await request.body()
    secret = os.environ.get("WEBHOOK_SECRET")
    if secret:
        ok = verify_github_signature(secret.encode(), body, x_hub_signature_256 or "")
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Idempotency and delivery id capture (stub)
    delivery_id = x_github_delivery or "unknown"

    # ENQUEUE: publish to Pub/Sub topic configured in PUBSUB_TOPIC
    try:
        topic = environ.get("PUBSUB_TOPIC")
        payload = {
            "delivery_id": delivery_id,
            "body": body.decode("utf-8", errors="replace"),
        }
        attrs = {"delivery_id": delivery_id}
        if topic:
            publish_webhook_message(topic, payload, attributes=attrs)
            logger.info("enqueued delivery=%s to topic=%s", delivery_id, topic)
        else:
            logger.warning(
                "PUBSUB_TOPIC not set; skipping enqueue for delivery=%s", delivery_id
            )
    except Exception:
        logger.exception("failed to enqueue delivery=%s", delivery_id)

    return Response(status_code=202)
