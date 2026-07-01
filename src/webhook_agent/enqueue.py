"""Pub/Sub enqueue helper for webhook messages.

Provides a non-blocking publish function used by the FastAPI webhook receiver.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.cloud import pubsub_v1

logger = logging.getLogger("webhook_enqueue")


def publish_webhook_message(
    topic_path: str, payload: dict[str, Any], attributes: dict[str, str] | None = None
) -> None:
    """Publish a JSON payload to a Pub/Sub topic. Non-blocking (fires publish and returns).

    topic_path: full topic name, e.g. projects/PROJECT/topics/TOPIC
    payload: JSON-serializable object
    attributes: optional string attributes
    """
    publisher = pubsub_v1.PublisherClient()
    data = json.dumps(payload).encode("utf-8")
    try:
        future = publisher.publish(topic_path, data, **(attributes or {}))

        # Do not block on publish; optionally attach callback to log result
        def _cb(fut):
            try:
                message_id = fut.result()
                logger.info("published message id=%s to %s", message_id, topic_path)
            except Exception:
                logger.exception("publish failed to %s", topic_path)

        future.add_done_callback(_cb)
    except Exception:
        logger.exception("failed to publish message to %s", topic_path)
