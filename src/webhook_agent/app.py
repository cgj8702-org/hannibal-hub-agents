import hashlib
import hmac
import logging
import os
import shutil
import stat
import subprocess
import urllib.request
from contextlib import asynccontextmanager
from os import environ

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


app = FastAPI(title="github-webhook-agent-starter", lifespan=lifespan)


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
