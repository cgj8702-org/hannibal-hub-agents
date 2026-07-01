"""Simple Pub/Sub worker skeleton for processing webhook jobs.

Usage:

  export PUBSUB_PROJECT=chatbot-project-hannibal
  export PUBSUB_SUBSCRIPTION=projects/$PUBSUB_PROJECT/subscriptions/hannibal-webhook-sub
  export GITHUB_APP_ID=12345
  export GITHUB_INSTALLATION_ID=67890
  export GITHUB_PRIVATE_KEY_PATH=/path/to/private-key.pem

  python src/webhook_agent/worker.py

This worker demonstrates:
- subscribing to a Pub/Sub subscription
- exchanging a GitHub App JWT for an installation token (using `github_app_credential_helper`)
- basic dead-letter publishing
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from typing import Any

from github import Auth, Github
from google.cloud import pubsub_v1

from github_app_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

logger = logging.getLogger("webhook_worker")
logging.basicConfig(level=logging.INFO)


def process_message_data(
    data: dict[str, Any], app_id: int, installation_id: int, private_key_path: str
) -> None:
    delivery_id = data.get("delivery_id", "unknown")
    logger.info(
        "processing delivery=%s payload_keys=%s", delivery_id, list(data.keys())
    )

    # Get cached installation token or request a new one
    inst_token = load_cached_token(installation_id)
    if inst_token is None:
        pem = load_private_key(private_key_path)
        jwt_token = generate_jwt(app_id, pem)
        inst_token = get_installation_token(jwt_token, installation_id)
        save_cached_token(installation_id, inst_token)

    # Use PyGitHub to perform actions. Use the new auth API to avoid deprecation warnings.
    gh = Github(auth=Auth.Token(inst_token.token))
    # This is a placeholder: adjust to fetch PR/issue context as needed
    logger.info(
        "authenticated as installation; token_expires=%s", inst_token.expires_at
    )

    # Agent core: make decisions and execute tools behind policy gates
    from .agent_core import AgentCore

    agent = AgentCore(
        gh_client=gh,
        dry_run=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
    )

    repo_name = data.get("repo")
    if repo_name:
        try:
            results = agent.run(data, repo_name)
            for r in results:
                logger.info("agent action completed: %s %s", r.tool, r.detail)
        except Exception as exc:
            logger.exception("agent core failed for repo %s: %s", repo_name, exc)


def publish_dead_letter(
    publisher: pubsub_v1.PublisherClient, topic: str, original_msg: bytes
) -> None:
    try:
        future = publisher.publish(topic, original_msg)
        _ = future.result(timeout=10.0)
        logger.info("published dead-letter to %s", topic)
    except Exception:
        logger.exception("failed to publish dead-letter to %s", topic)


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
        app_id = int(os.environ["GITHUB_APP_ID"])
        installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
        private_key_path = os.environ["GITHUB_PRIVATE_KEY_PATH"]
    except KeyError as e:
        print(f"Missing environment variable: {e}", file=sys.stderr)
        return 3

    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    streaming_pull_future = None

    def callback(message: pubsub_v1.subscriber.message.Message) -> None:  # type: ignore[override]
        logger.info("received message: %s", message.message_id)
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
            process_message_data(payload, app_id, installation_id, private_key_path)
            message.ack()
            logger.info("acked message %s", message.message_id)
        except Exception:
            logger.exception("processing failed for message %s", message.message_id)
            if dead_letter_topic:
                publish_dead_letter(publisher, dead_letter_topic, message.data)
                message.ack()
            else:
                # Let Pub/Sub redeliver by not acking
                logger.info("not acking message to allow retry")

    subscription_path = subscription

    logger.info("starting subscriber for %s", subscription_path)
    streaming_pull_future = subscriber.subscribe(subscription_path, callback=callback)

    # Graceful shutdown handling
    def _signal_handler(signum, frame):
        logger.info("signal %s received, cancelling subscriber", signum)
        streaming_pull_future.cancel()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        streaming_pull_future.result()
    except Exception:
        logger.exception("subscriber terminated")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
