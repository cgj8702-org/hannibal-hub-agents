"""Publish a test webhook event to the configured Pub/Sub topic.

Publishes a properly normalized event that exercises the event router,
loop-avoidance, and agent core in a single end-to-end flow.

Usage:
  export PUBSUB_TOPIC=projects/cgj8702-webhook-agent/topics/webhooks
  uv run python scripts/publish_test_message.py

Requires: PUBSUB_TOPIC and GOOGLE_APPLICATION_CREDENTIALS in env
"""

from __future__ import annotations

import json
import os

from google.cloud import pubsub_v1


def main() -> None:
    topic = os.environ.get("PUBSUB_TOPIC")
    if not topic:
        raise SystemExit("PUBSUB_TOPIC must be set in the environment")

    # Normalized event payload matching the shape produced by app.py::normalize_payload
    # This exercises: route_event -> "pull_request.opened", should_process_event -> True,
    # and agent core rule-based planning -> add_comment action
    payload = {
        "delivery_id": "e2e-test-delivery-001",
        "event_name": "pull_request",
        "action": "opened",
        "sender": {"login": "test-human-user", "type": "User", "id": 12345},
        "installation": {"id": 67890},
        "repository": {
            "full_name": "cgj8702/chatbot-repo",
            "owner": {"login": "cgj8702"},
        },
        "raw_payload": {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "e2e test PR",
                "head": {"ref": "test-branch", "sha": "abc123"},
                "base": {"ref": "main", "sha": "def456"},
            },
        },
    }

    publisher = pubsub_v1.PublisherClient()
    future = publisher.publish(
        topic,
        json.dumps(payload).encode("utf-8"),
        delivery_id=payload["delivery_id"],
        event_name=payload["event_name"],
        action=payload["action"],
    )
    message_id = future.result()
    print(f"published message_id={message_id} delivery_id={payload['delivery_id']}")


if __name__ == "__main__":
    main()
