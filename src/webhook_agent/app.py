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
import shutil
import stat
import subprocess
import urllib.request
from contextlib import asynccontextmanager
from os import environ
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .enqueue import publish_webhook_message

logger = logging.getLogger("webhook_receiver")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cf_token = os.environ.get("CF_TUNNEL_TOKEN")
    tunnel_process = None
    if cf_token:
        logger.info("CF_TUNNEL_TOKEN found. Preparing Cloudflare Tunnel...")

        cf_bin = shutil.which("cloudflared")
        if not cf_bin:
            cf_bin = "/tmp/cloudflared"
            if not os.path.exists(cf_bin):
                logger.info(
                    "cloudflared not found in PATH. Downloading standalone binary..."
                )
                url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
                try:
                    urllib.request.urlretrieve(url, cf_bin)
                    st = os.stat(cf_bin)
                    os.chmod(cf_bin, st.st_mode | stat.S_IEXEC)
                    logger.info("cloudflared binary downloaded and made executable.")
                except Exception as e:
                    logger.error(f"Failed to download cloudflared: {e}")
                    cf_bin = None

        if cf_bin:
            logger.info("Starting Cloudflare Tunnel...")
            log_file = "/tmp/cloudflared_tunnel.log"
            try:
                tunnel_process = subprocess.Popen(
                    [cf_bin, "tunnel", "run", "--token", cf_token],
                    stdout=open(log_file, "w"),
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                logger.info(
                    f"Cloudflare Tunnel process started. Logs redirected to {log_file}"
                )
            except Exception as e:
                logger.error(f"Failed to start Cloudflare Tunnel: {e}")
        else:
            logger.error(
                "Cloudflare Tunnel cannot be started because cloudflared binary is missing."
            )

    yield

    # Shutdown
    if tunnel_process:
        logger.info("Terminating Cloudflare Tunnel process...")
        tunnel_process.terminate()
        try:
            tunnel_process.wait(timeout=5)
            logger.info("Cloudflare Tunnel process terminated.")
        except subprocess.TimeoutExpired:
            logger.warning("Cloudflare Tunnel process did not terminate. Killing...")
            tunnel_process.kill()
            logger.info("Cloudflare Tunnel process killed.")


app = FastAPI(title="hannibal-hub-webhook-agent", lifespan=lifespan)


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
