import hashlib
import hmac
import json
import logging
import os

import functions_framework
from google.cloud import pubsub_v1

logger = logging.getLogger(__name__)

# Initialize the Pub/Sub client globally to reuse it across requests
project_id = os.environ.get("PUBSUB_PROJECT")
topic_id = os.environ.get("PUBSUB_TOPIC")
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(project_id, topic_id)

# The secret token you configured in your GitHub App settings
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
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


def normalize_payload(event_name: str, raw_payload: dict, delivery_id: str) -> dict:
    """Normalize incoming webhook payload into the project's consistent event envelope."""
    return {
        "delivery_id": delivery_id,
        "event_name": event_name,
        "action": raw_payload.get("action"),
        "sender": raw_payload.get("sender"),
        "installation": raw_payload.get("installation"),
        "repository": raw_payload.get("repository"),
        "raw_payload": raw_payload,
    }


@functions_framework.http
def github_webhook_router(request):
    # Reject anything that isn't a POST request from GitHub
    if request.method != "POST":
        return "Method Not Allowed", 405

    signature_header = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
    payload_body = request.get_data()

    # 1. Verify that the webhook actually came from GitHub
    if not verify_signature(payload_body, signature_header):
        return "Unauthorized: Signature mismatch", 401

    try:
        # 2. Parse and Normalize
        raw_payload = json.loads(payload_body.decode("utf-8", errors="replace"))
        normalized = normalize_payload(event_type, raw_payload, delivery_id)

        # Convert normalized dict to bytes for Pub/Sub
        data_bytes = json.dumps(normalized).encode("utf-8")

        # 3. Publish to Pub/Sub with full metadata for filtering/logging
        future = publisher.publish(
            topic_path,
            data=data_bytes,
            delivery_id=delivery_id,
            event_name=str(event_type),
            action=str(normalized["action"] or ""),
        )
        future.result()
    except Exception as e:
        logger.exception("Error processing or publishing to Pub/Sub: %s", e)
        return "Internal Server Error", 500

    # Respond to GitHub immediately
    return "Event normalized and queued successfully", 202
