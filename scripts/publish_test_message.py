"""Publish a test message to the configured Pub/Sub topic.

Usage:
  python3 scripts/publish_test_message.py
Requires: PUBSUB_TOPIC and GOOGLE_APPLICATION_CREDENTIALS in env
"""

import json
import os

from google.cloud import pubsub_v1


def main():
    topic = os.environ.get("PUBSUB_TOPIC")
    if not topic:
        raise SystemExit("PUBSUB_TOPIC must be set in the environment")
    publisher = pubsub_v1.PublisherClient()
    payload = {
        "delivery_id": "test-delivery-1",
        "repo": "cgj8702/chatbot-repo",
        "test": True,
        # ask the worker to perform a safe writeback (create an issue) when ALLOW_GITHUB_WRITEBACK=1
        "writeback_create_issue": True,
        "writeback_title": "[agent test] webhook processed",
        "writeback_body": "This issue was created by the webhook-agent test to verify writeback.",
    }
    future = publisher.publish(topic, json.dumps(payload).encode("utf-8"))
    message_id = future.result()
    print("published", message_id)


if __name__ == "__main__":
    main()
