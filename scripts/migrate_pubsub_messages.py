"""Migrate backlogged Pub/Sub messages from a source subscription to a target topic.

This script pulls messages from a source subscription (in an existing project),
re-publishes them to a target topic (in the new dedicated project), and acknowledges
the messages in the source subscription upon successful publishing.

Usage:
    export SOURCE_SUBSCRIPTION=projects/chatbot-project-hannibal/subscriptions/webhook-sub
    export TARGET_TOPIC=projects/new-webhook-project/topics/webhook
    uv run python scripts/migrate_pubsub_messages.py

    # Or with CLI flags:
    uv run python scripts/migrate_pubsub_messages.py \\
        --source-subscription projects/old-project/subscriptions/webhook-sub \\
        --target-topic projects/new-project/topics/webhook \\
        --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

from google.api_core import exceptions as gcp_exceptions
from google.cloud import pubsub_v1


def migrate_messages(
    source_subscription: str,
    target_topic: str,
    batch_size: int = 50,
    dry_run: bool = False,
) -> int:
    """Pull backlogged messages from source_subscription and publish to target_topic."""
    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    total_migrated = 0

    print("Starting Pub/Sub message migration:")
    print(f"  Source Subscription: {source_subscription}")
    print(f"  Target Topic:        {target_topic}")
    print(f"  Batch Size:          {batch_size}")
    print(f"  Dry Run:             {dry_run}\n")

    while True:
        try:
            response = subscriber.pull(
                request={
                    "subscription": source_subscription,
                    "max_messages": batch_size,
                },
                timeout=10.0,
            )
        except gcp_exceptions.DeadlineExceeded:
            print("Pull timeout reached. No more backlogged messages found.")
            break
        except gcp_exceptions.GoogleAPICallError as exc:
            print(f"GCP API Call Error during pull: {exc}", file=sys.stderr)
            break
        except Exception as exc:
            print(f"Unexpected error during pull: {exc}", file=sys.stderr)
            break

        if not response.received_messages:
            print("No messages returned from subscription. Migration finished.")
            break

        received_count = len(response.received_messages)
        print(f"Pulled batch of {received_count} message(s)...")

        ack_ids_to_ack: list[str] = []

        for item in response.received_messages:
            msg = item.message
            ack_id = item.ack_id
            msg_id = msg.message_id
            data = msg.data
            attributes = dict(msg.attributes) if msg.attributes else {}
            ordering_key = msg.ordering_key or ""

            if dry_run:
                print(
                    f"  [DRY RUN] Would migrate message {msg_id} (bytes: {len(data)}, attributes: {attributes})"
                )
                continue

            try:
                publish_kwargs: dict[str, str] = dict(attributes)
                if ordering_key:
                    future = publisher.publish(
                        target_topic, data, ordering_key=ordering_key, **publish_kwargs
                    )
                else:
                    future = publisher.publish(target_topic, data, **publish_kwargs)

                new_msg_id = future.result(timeout=10.0)
                ack_ids_to_ack.append(ack_id)
                total_migrated += 1
                print(f"  Migrated message {msg_id} -> target msg_id {new_msg_id}")
            except Exception as exc:
                print(f"  Failed to publish message {msg_id}: {exc}", file=sys.stderr)

        if ack_ids_to_ack and not dry_run:
            subscriber.acknowledge(
                request={"subscription": source_subscription, "ack_ids": ack_ids_to_ack}
            )
            print(
                f"  Acknowledged {len(ack_ids_to_ack)} message(s) in source subscription."
            )

    print(f"\nMigration complete. Total messages migrated: {total_migrated}")
    return total_migrated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate backlogged Pub/Sub messages to a new target topic."
    )
    parser.add_argument(
        "--source-subscription",
        default=os.environ.get("SOURCE_SUBSCRIPTION")
        or os.environ.get("PUBSUB_SUBSCRIPTION"),
        help="Full path to source subscription (e.g. projects/OLD_PROJECT/subscriptions/webhook-sub)",
    )
    parser.add_argument(
        "--target-topic",
        default=os.environ.get("TARGET_TOPIC") or os.environ.get("PUBSUB_TOPIC"),
        help="Full path to target topic (e.g. projects/NEW_PROJECT/topics/webhook)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Maximum messages to pull per batch (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pull and display messages without publishing or acknowledging them",
    )

    args = parser.parse_args()

    if not args.source_subscription:
        print(
            "Error: --source-subscription or SOURCE_SUBSCRIPTION / PUBSUB_SUBSCRIPTION env var is required.",
            file=sys.stderr,
        )
        return 1
    if not args.target_topic and not args.dry_run:
        print(
            "Error: --target-topic or TARGET_TOPIC / PUBSUB_TOPIC env var is required.",
            file=sys.stderr,
        )
        return 1

    migrate_messages(
        source_subscription=args.source_subscription,
        target_topic=args.target_topic or "",
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
