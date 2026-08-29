"""Pub/Sub worker — subscribes to events and delegates processing to WebhookProcessor.

This worker subscribes to a Pub/Sub subscription, processes each normalized
webhook event, and leverages the WebhookProcessor to handle routing, loop protection,
and agent execution.

Usage:
  export PUBSUB_PROJECT=cgj8702-webhook-agent
  export PUBSUB_SUBSCRIPTION=projects/$PUBSUB_PROJECT/subscriptions/webhooks-sub
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

from google.api_core import exceptions as gcp_exceptions
from google.cloud import pubsub_v1

from .processor import WebhookProcessor

# Ensure src/ is on sys.path so the package is importable when run as a script
_src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)


logger = logging.getLogger("webhook_agent.worker")
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
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


def setup_cloud_logging() -> None:
    """Initialize Google Cloud Logging handler if available."""
    from logic.constants import DEFAULT_PUBSUB_PROJECT

    try:
        import google.cloud.logging

        project_id = os.environ.get("PUBSUB_PROJECT", DEFAULT_PUBSUB_PROJECT)
        client = google.cloud.logging.Client(project=project_id)
        client.setup_logging(log_level=logging.DEBUG)
        logger.info("☁️ Google Cloud Logging initialized for project [%s]", project_id)
    except Exception as exc:
        logger.warning("Could not initialize Google Cloud Logging handler: %s", exc)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> int:
    from logic.constants import (
        DEFAULT_PUBSUB_DEAD_LETTER_TOPIC,
        DEFAULT_PUBSUB_PROJECT,
        DEFAULT_PUBSUB_SUBSCRIPTION,
    )

    setup_cloud_logging()
    os.environ.get("PUBSUB_PROJECT", DEFAULT_PUBSUB_PROJECT)
    subscription = os.environ.get("PUBSUB_SUBSCRIPTION", DEFAULT_PUBSUB_SUBSCRIPTION)
    dead_letter_topic = os.environ.get(
        "PUBSUB_DEAD_LETTER_TOPIC", DEFAULT_PUBSUB_DEAD_LETTER_TOPIC
    )

    try:
        # We instantiate the processor here to validate environment variables early
        processor = WebhookProcessor()
    except KeyError as e:
        print(f"Missing environment variable: {e}", file=sys.stderr)
        return 3

    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    subscription_path = subscription
    keep_running = True

    def _signal_handler(signum, frame):
        nonlocal keep_running
        logger.info("🛑 Signal %s received, shutting down...", signum)
        keep_running = False

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    PROACTIVE_SWEEP_INTERVAL_SECONDS = 1800  # 30 minutes
    last_proactive_sweep = 0.0

    logger.info("🚀 Starting sequential subscriber loop on %s", subscription_path)
    while keep_running:
        # Periodic Proactive Agent Sweep (Every 30 minutes)
        import time

        now = time.time()
        if now - last_proactive_sweep >= PROACTIVE_SWEEP_INTERVAL_SECONDS:
            last_proactive_sweep = now
            try:
                import threading
                from .proactive_service import ProactiveEvaluator

                target_repo = os.environ.get(
                    "GITHUB_REPOSITORY", "cgj8702-org/hannibal-hub-agents"
                )
                evaluator = ProactiveEvaluator(processor.gh, target_repo)
                sweep_thread = threading.Thread(
                    target=evaluator.evaluate_open_prs,
                    name="ProactiveSweepWorker",
                    daemon=True,
                )
                sweep_thread.start()
            except Exception as exc:
                logger.warning("Proactive background sweep skipped/failed: %s", exc)

        try:
            # Pull exactly one message synchronously
            response = subscriber.pull(
                request={"subscription": subscription_path, "max_messages": 1},
                timeout=30.0,
            )
        except gcp_exceptions.DeadlineExceeded:
            # Transient deadline error - just continue polling
            logger.debug("⏱️ Pull deadline exceeded, continuing...")
            continue
        except gcp_exceptions.GoogleAPICallError as e:
            # Retryable API error - log and continue
            logger.warning("📡 API call error during pull: %s", e)
            continue
        except Exception:
            # Unexpected error during pull - log and continue
            logger.exception("💥 Unexpected error during pull, continuing...")
            continue

        if not response.received_messages:
            continue

        message = response.received_messages[0].message
        ack_id = response.received_messages[0].ack_id

        logger.debug("📥 Received message: %s", str(message.message_id)[-4:])

        try:
            payload = json.loads(message.data.decode())
            # Delegate all routing, filtering, and execution to the processor
            # This is a blocking call; the loop waits until it's done
            processor.process_event(payload)

            subscriber.acknowledge(
                request={"subscription": subscription_path, "ack_ids": [ack_id]}
            )
            logger.debug("✅ Acked message: %s", str(message.message_id)[-4:])
        except Exception:
            logger.exception(
                "💥 Processing failed for message %s",
                str(message.message_id)[-4:],
            )
            if dead_letter_topic:
                publish_dead_letter(publisher, dead_letter_topic, message.data)
                subscriber.acknowledge(
                    request={"subscription": subscription_path, "ack_ids": [ack_id]}
                )
            else:
                # Let Pub/Sub redeliver by not acking
                logger.debug("🔄 Not acking message to allow retry")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
