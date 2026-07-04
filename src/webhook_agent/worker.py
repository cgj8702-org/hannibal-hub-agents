"""Pub/Sub worker — subscribes to events and delegates processing to WebhookProcessor.

This worker subscribes to a Pub/Sub subscription, processes each normalized
webhook event, and leverages the WebhookProcessor to handle routing, loop protection,
and agent execution.

Usage:
  export PUBSUB_PROJECT=chatbot-project-hannibal
  export PUBSUB_SUBSCRIPTION=projects/$PUBSUB_PROJECT/subscriptions/hannibal-webhook-sub
  export GITHUB_APP_ID=12345
  export GITHUB_INSTALLATION_ID=67890
  export GITHUB_PRIVATE_KEY_PATH=/path/to/private-key.pem

  python src/webhook_agent/worker.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys

from google.cloud import pubsub_v1
from .processor import WebhookProcessor

# Ensure src/ is on sys.path so the package is importable when run as a script
_src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)


logger = logging.getLogger("worker")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

# Silence verbose third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Dead-letter publishing
# ---------------------------------------------------------------------------
def publish_dead_letter(
    publisher: pubsub_v1.PublisherClient, topic: str, original_msg: bytes
) -> None:
    try:
        future = publisher.publish(topic, original_msg)
        _ = future.result(timeout=10.0)
        logger.debug("💀 Published dead-letter to %s", topic)
    except Exception:
        logger.exception("💥 Failed to publish dead-letter to %s", topic)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> int:
    os.environ.get("PUBSUB_PROJECT", "chatbot-project-hannibal")
    subscription = os.environ.get("PUBSUB_SUBSCRIPTION")
    dead_letter_topic = os.environ.get("PUBSUB_DEAD_LETTER_TOPIC")

    if not subscription:
        print(
            "PUBSUB_SUBSCRIPTION must be set to the full subscription path",
            file=sys.stderr,
        )
        return 2

    try:
        # We instantiate the processor here to validate environment variables early
        processor = WebhookProcessor()
    except KeyError as e:
        print(f"Missing environment variable: {e}", file=sys.stderr)
        return 3

    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    streaming_pull_future = None

    def callback(message: pubsub_v1.subscriber.message.Message) -> None:
        logger.debug("📥 Received message: %s", str(message.message_id)[-4:])
        try:
            payload = json.loads(message.data.decode())
        except Exception:
            logger.exception(
                "invalid JSON payload; sending to dead-letter if configured"
            )
            if dead_letter_topic:
                publish_dead_letter(publisher, dead_letter_topic, message.data)
            message.ack()
            return

        try:
            # Delegate all routing, filtering, and execution to the processor
            processor.process_event(payload)
            message.ack()
            logger.debug("✅ Acked message: %s", str(message.message_id)[-4:])
        except Exception:
            logger.exception("💥 Processing failed for message %s", message.message_id)
            if dead_letter_topic:
                publish_dead_letter(publisher, dead_letter_topic, message.data)
                message.ack()
            else:
                # Let Pub/Sub redeliver by not acking
                logger.debug("🔄 Not acking message to allow retry")

    subscription_path = subscription

    logger.debug("🚀 Starting subscriber")
    streaming_pull_future = subscriber.subscribe(subscription_path, callback=callback)

    # Graceful shutdown handling
    def _signal_handler(signum, frame):
        logger.info("🛑 Signal %s received, shutting down...", signum)
        streaming_pull_future.cancel()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        streaming_pull_future.result()
    except Exception:
        logger.exception("💥 Subscriber terminated unexpectedly")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
