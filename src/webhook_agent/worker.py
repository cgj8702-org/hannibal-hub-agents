"""Pub/Sub worker — routes canonical events, enforces loop protection, and calls the agent core.

This worker subscribes to a Pub/Sub subscription, processes each normalized
webhook event, and routes it through the agent core for planning and execution.

Canonical event categories (produced by :func:`route_event`):

-- ``pull_request.opened``
-- ``pull_request.synchronize``
-- ``pull_request.closed``
-- ``pull_request.ready_for_review``
-- ``issue_comment.created``
-- ``pull_request_review_comment.created``
-- ``pull_request_review.submitted``
-- ``pull_request_review_requested``
-- ``label.created`` / ``label.deleted``
-- ``installation.created`` / ``installation.deleted`` / ``installation.suspend`` / ``installation.unsuspend``
-- ``unknown`` (fallback for unhandled event/action combos)

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
from typing import Any

from github import Auth, Github
from google.cloud import pubsub_v1

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
# Bot identity — used for loop-avoidance
# ---------------------------------------------------------------------------
BOT_LOGIN = "hannibal-hub-agents[bot]"

# ---------------------------------------------------------------------------
# Processed delivery ID tracking (in-memory dedupe set)
# ---------------------------------------------------------------------------
_processed_deliveries: set[str] = set()


def _is_bot_actor(sender: dict[str, Any] | None) -> bool:
    """Return True if the event sender is the bot itself."""
    if sender is None:
        return False
    return sender.get("login") == BOT_LOGIN


def _is_bot_comment_author(comment: dict[str, Any] | None) -> bool:
    """Return True if a comment/review was authored by the bot."""
    if comment is None:
        return False
    user = comment.get("user") or {}
    return user.get("login") == BOT_LOGIN


# ---------------------------------------------------------------------------
# Canonical event routing
# ---------------------------------------------------------------------------
def route_event(normalized: dict[str, Any]) -> str:
    """Map a normalized webhook event to a canonical internal event category.

    Returns a string like ``pull_request.opened`` or ``unknown``.
    """
    event_name = normalized.get("event_name", "")
    action = normalized.get("action") or ""
    raw = normalized.get("raw_payload", {})

    # --- pull_request_review_requested (via pull_request action) ---
    # Must be checked before the generic pull_request handler below.
    if event_name == "pull_request" and action == "review_requested":
        return "pull_request_review_requested"

    # --- pull_request events ---
    if event_name == "pull_request":
        pr_action = raw.get("action", "")
        if pr_action in (
            "opened",
            "synchronize",
            "closed",
            "ready_for_review",
            "reopened",
        ):
            return f"pull_request.{pr_action}"
        return f"pull_request.{pr_action}" if pr_action else "pull_request"

    # --- issue_comment events ---
    if event_name == "issue_comment":
        return f"issue_comment.{action}" if action else "issue_comment"

    # --- pull_request_review_comment events ---
    if event_name == "pull_request_review_comment":
        return (
            f"pull_request_review_comment.{action}"
            if action
            else "pull_request_review_comment"
        )

    # --- pull_request_review events ---
    if event_name == "pull_request_review":
        return f"pull_request_review.{action}" if action else "pull_request_review"

    # --- label events ---
    if event_name == "label":
        return f"label.{action}" if action else "label"

    # --- installation events ---
    if event_name == "installation":
        return f"installation.{action}" if action else "installation"

    # --- ping (heartbeat) ---
    if event_name == "ping":
        return "ping"

    return "unknown"


# ---------------------------------------------------------------------------
# Ignored event types — skipped immediately to save API calls
# ---------------------------------------------------------------------------
IGNORED_EVENTS: set[str] = {
    "pull_request.closed",
    "pull_request.synchronize",
    "check_suite.requested",
    "check_suite.completed",
    "issue_comment.deleted",
    "label.created",
    "label.deleted",
}


# ---------------------------------------------------------------------------
# Loop-avoidance and dedupe check
# ---------------------------------------------------------------------------
def should_process_event(normalized: dict[str, Any]) -> bool:
    """Determine whether the event should be processed or suppressed.

    Suppression rules:
    0. Ignored event types (configured list).
    1. Already processed delivery ID (dedupe).
    2. Sender is the bot itself (loop avoidance), unless the event is an
       explicit follow-up action we want to allow.
    3. For comment/review events, check if the comment author is the bot.
    """
    delivery_id = normalized.get("delivery_id", "")
    canonical = route_event(normalized)

    # Rule 0: ignored event types
    if canonical in IGNORED_EVENTS:
        logger.info(
            "🤫 Ignoring event by configuration: %s",
            canonical.replace("_", " ").replace(".", " ").capitalize(),
        )
        return False

    # Rule 1: dedupe
    if delivery_id in _processed_deliveries:
        logger.debug("🛡️  Suppressed duplicate delivery: %s", delivery_id)
        return False

    # Rule 2: bot actor suppression
    sender = normalized.get("sender")
    if _is_bot_actor(sender):
        event_name = normalized.get("event_name", "")
        action = normalized.get("action") or ""
        # Allowlist of follow-up events the bot is allowed to act on
        # (currently empty — expand as needed for deliberate follow-ups)
        allowed_followups: set[str] = set()
        canonical = f"{event_name}.{action}" if action else event_name
        if canonical not in allowed_followups:
            logger.debug(
                "🛡️  Suppressed bot-authored event: %s %s %s",
                delivery_id,
                canonical.replace("_", " ").replace(".", " ").capitalize(),
                sender.get("login"),
            )
            return False

    # Rule 3: bot comment/review author suppression
    raw = normalized.get("raw_payload", {})
    comment = raw.get("comment") or raw.get("review")
    if _is_bot_comment_author(comment):
        logger.debug("🛡️  Suppressed bot-authored comment/review: %s", delivery_id)
        return False

    return True


def mark_processed(delivery_id: str) -> None:
    """Record a delivery ID as processed (for dedupe)."""
    _processed_deliveries.add(delivery_id)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------
def process_message_data(
    data: dict[str, Any], app_id: int, installation_id: int, private_key_path: str
) -> None:
    from webhook_agent.agent_core import AgentCore
    from webhook_agent.github_credential_helper import (
        generate_jwt,
        get_installation_token,
        load_cached_token,
        load_private_key,
        save_cached_token,
    )

    delivery_id = data.get("delivery_id", "unknown")
    canonical = route_event(data)

    logger.info(
        "⚙️  Processing event: %s",
        canonical.replace("_", " ").replace(".", " ").capitalize(),
    )

    # Loop-avoidance and dedupe
    if not should_process_event(data):
        return

    # Mark as processed
    mark_processed(delivery_id)

    # Get cached installation token or request a new one
    inst_token = load_cached_token(installation_id)
    if inst_token is None:
        pem = load_private_key(private_key_path)
        jwt_token = generate_jwt(app_id, pem)
        inst_token = get_installation_token(jwt_token, installation_id)
        save_cached_token(installation_id, inst_token)

    # Use PyGitHub to perform actions
    gh = Github(auth=Auth.Token(inst_token.token))
    logger.debug(
        "🔑 Authenticated as installation (expires: %s)", inst_token.expires_at
    )

    # Agent core: make decisions and execute tools behind policy gates
    agent = AgentCore(
        gh_client=gh,
        dry_run=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"),
    )

    repo_name = (
        data.get("repository", {}).get("full_name") if data.get("repository") else None
    )
    if not repo_name:
        logger.warning("⚠️  No repository found in event delivery: %s", delivery_id)
        return

    try:
        results = agent.run(data, repo_name)
        for r in results:
            status_symbol = "🤖" if r.success else "❌"
            logger.info("%s Agent action: %s", status_symbol, r.detail)
    except Exception:
        logger.exception("💥 Agent core failed for repo %s", repo_name)


# ---------------------------------------------------------------------------
# Dead-letter publishing
# ---------------------------------------------------------------------------
def publish_dead_letter(
    publisher: pubsub_v1.PublisherClient, topic: str, original_msg: bytes
) -> None:
    try:
        future = publisher.publish(topic, original_msg)
        _ = future.result(timeout=10.0)
        logger.info("💀 Published dead-letter to %s", topic)
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
        app_id = int(os.environ["GITHUB_APP_ID"])
        installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
        private_key_path = os.environ["GITHUB_PRIVATE_KEY_PATH"]
    except KeyError as e:
        print(f"Missing environment variable: {e}", file=sys.stderr)
        return 3

    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    streaming_pull_future = None

    def callback(message: pubsub_v1.subscriber.message.Message) -> None:
        logger.info("📥 Received message: %s", str(message.message_id)[-4:])
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
            logger.info("✅ Acked message: %s", str(message.message_id)[-4:])
        except Exception:
            logger.exception("💥 Processing failed for message %s", message.message_id)
            if dead_letter_topic:
                publish_dead_letter(publisher, dead_letter_topic, message.data)
                message.ack()
            else:
                # Let Pub/Sub redeliver by not acking
                logger.info("🔄 Not acking message to allow retry")

    subscription_path = subscription

    logger.info("🚀 Starting subscriber")
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
