"""FastAPI webhook receiver — normalizes incoming GitHub webhook payloads.

Every delivery is parsed, normalized to a consistent shape, and placed in an
internal queue for asynchronous processing by the WebhookProcessor.

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

import asyncio
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
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .processor import WebhookProcessor

logger = logging.getLogger("app")

# Silence verbose third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)

# Global state for the queue and processor
state = {
    "queue": asyncio.Queue(),
    "processor": None,
    "worker_task": None,
}


async def webhook_worker():
    """Background worker that consumes events from the queue."""
    logger.info("🚀 Webhook background worker started.")
    processor = state["processor"]
    while True:
        try:
            # Wait for an event from the queue
            event = await state["queue"].get()
            try:
                # Process the event (this is a synchronous call, so we run it in a thread)
                await asyncio.to_thread(processor.process_event, event)
            except (RuntimeError, ConnectionError, TimeoutError):
                logger.exception("💥 Recoverable error processing event from queue")
            except Exception:
                logger.exception("💥 Unexpected error processing event from queue")
            finally:
                state["queue"].task_done()
        except asyncio.CancelledError:
            logger.info("🛑 Webhook background worker shutting down.")
            break
        except Exception:
            logger.exception("💥 Unexpected error in webhook worker loop")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Initialize Processor
    try:
        state["processor"] = WebhookProcessor()
    except (KeyError, ValueError, TypeError) as e:
        logger.error(
            f"💥 Failed to initialize WebhookProcessor due to config error: {e}"
        )
        # We don't raise here to allow the server to start, but webhooks will fail

    # Start the background worker
    state["worker_task"] = asyncio.create_task(webhook_worker())

    # Cloudflare Tunnel Setup
    cf_token = os.environ.get("CF_TUNNEL_TOKEN")
    tunnel_process = None
    if cf_token:
        logger.info("☁️  CF_TUNNEL_TOKEN found. Preparing Cloudflare Tunnel...")

        cf_bin = shutil.which("cloudflared")
        if not cf_bin:
            cf_bin = "/tmp/cloudflared"
            if not os.path.exists(cf_bin):
                logger.info(
                    "📥 cloudflared not found in PATH. Downloading standalone binary..."
                )
                url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
                try:
                    urllib.request.urlretrieve(url, cf_bin)
                    st = os.stat(cf_bin)
                    os.chmod(cf_bin, st.st_mode | stat.S_IEXEC)
                    logger.info("✅ cloudflared binary downloaded and made executable.")
                except (urllib.error.URLError, OSError) as e:
                    logger.error(f"💥 Failed to download cloudflared: {e}")
                    cf_bin = None

        if cf_bin:
            logger.info("🚀 Starting Cloudflare Tunnel...")
            log_file = "/tmp/cloudflared_tunnel.log"
            try:
                # Use asyncio.to_thread to avoid blocking the event loop with Popen and open()
                def start_tunnel():
                    with open(log_file, "w") as f:
                        return subprocess.Popen(
                            [cf_bin, "tunnel", "run", "--token", cf_token],
                            stdout=f,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )

                tunnel_process = await asyncio.to_thread(start_tunnel)
                logger.info(
                    f"✅ Cloudflare Tunnel process started. Logs redirected to {log_file}"
                )
            except (OSError, subprocess.SubprocessError) as e:
                logger.error(f"💥 Failed to start Cloudflare Tunnel: {e}")
        else:
            logger.error(
                "💥 Cloudflare Tunnel cannot be started because cloudflared binary is missing."
            )

    yield

    # Shutdown
    if state["worker_task"]:
        logger.info("🛑 Cancelling background worker...")
        state["worker_task"].cancel()
        try:
            await state["worker_task"]
        except asyncio.CancelledError:
            pass

    if tunnel_process:
        logger.info("🛑 Terminating Cloudflare Tunnel process...")
        tunnel_process.terminate()
        try:
            tunnel_process.wait(timeout=5)
            logger.info("✅ Cloudflare Tunnel process terminated.")
        except subprocess.TimeoutExpired:
            logger.warning("⚠️  Cloudflare Tunnel process did not terminate. Killing...")
            tunnel_process.kill()
            logger.info("💀 Cloudflare Tunnel process killed.")


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
    """Normalize incoming webhook payload into a consistent event envelope."""
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
    4. Enqueue the normalized event for background processing
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
        logger.error("💥 Failed to parse webhook body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Normalize the event for downstream consumers
    normalized = normalize_payload(
        event_name=event_name,
        raw_payload=raw_payload,
        delivery_id=delivery_id,
    )

    logger.info(
        "📥 Received event: event=%s action=%s delivery=%s sender=%s repo=%s",
        normalized["event_name"],
        normalized["action"],
        delivery_id,
        normalized["sender"].get("login") if normalized["sender"] else None,
        normalized["repository"].get("full_name") if normalized["repository"] else None,
    )

    # Enqueue for background processing
    try:
        state["queue"].put_nowait(normalized)
        logger.info(
            "📨 Enqueued delivery: delivery=%s event=%s", delivery_id, event_name
        )
    except asyncio.QueueFull:
        logger.error("💥 Queue full; failed to enqueue delivery: %s", delivery_id)
        # We still return 202 because we don't want to block GitHub,
        # but the event is lost if the queue is full/broken.

    return Response(status_code=202)
