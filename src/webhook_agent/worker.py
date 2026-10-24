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

import contextlib
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import warnings
from typing import Any

import google.cloud.pubsub_v1 as pubsub_v1
import httpx
from github import GithubException
from google.api_core import exceptions as gcp_exceptions

from .processor import WebhookProcessor

# Ensure src/ is on sys.path so the package is importable when run as a script
_src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)


logger = logging.getLogger("webhook_agent.worker")

# Delays for retrying transient failures (±20% jitter applied at runtime)
_RETRY_DELAYS = (5.0, 20.0)

# Silence verbose third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
logging.getLogger("google.auth").setLevel(logging.WARNING)
logging.getLogger("google_adk").setLevel(logging.ERROR)
logging.getLogger("google.adk").setLevel(logging.ERROR)
# ADK's Runner logs 'Root node failed' with exc_info on every failed invocation,
# duplicating the exception our orchestrator already logs once at the retry
# boundary. Suppress the per-attempt duplicate traceback while keeping other
# ADK ERROR records visible.
logging.getLogger("google_adk.google.adk.runners").setLevel(logging.CRITICAL)
# ADK's _node_runner logs 'Node execution failed with exception' + full
# traceback on every transient error; our execution.py already logs the same
# error with structured context at the retry boundary.
logging.getLogger("google_adk.google.adk.workflow").setLevel(logging.CRITICAL)
# Silence repetitive ADK experimental feature warnings
warnings.filterwarnings("ignore", category=UserWarning, module=r"google\.adk")


# ---------------------------------------------------------------------------
# Dead-letter publishing & transient error detection
# ---------------------------------------------------------------------------
def publish_dead_letter(
    publisher: pubsub_v1.PublisherClient, topic: str, original_msg: bytes
) -> bool:
    """Publish a failed message to the dead-letter topic. Returns True on success."""
    try:
        future = publisher.publish(topic, original_msg)
        _ = future.result(timeout=10.0)
        logger.debug("💀 Published dead-letter to %s", topic)
        return True
    except Exception:
        logger.exception("💥 Failed to publish dead-letter to %s", topic)
        return False


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception represents a transient failure eligible for retry."""
    if isinstance(
        exc,
        (
            gcp_exceptions.ServiceUnavailable,
            gcp_exceptions.ResourceExhausted,
            gcp_exceptions.DeadlineExceeded,
            gcp_exceptions.InternalServerError,
        ),
    ):
        return True
    if isinstance(exc, GithubException):
        if exc.status in (429, 500, 502, 503, 504):
            return True
        if exc.status == 403 and "rate limit" in str(exc).lower():
            return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        from google.genai.errors import ServerError

        if isinstance(exc, ServerError):
            return True
    except ImportError:
        pass
    return False


# ---------------------------------------------------------------------------
# Pub/Sub Ack Deadline Heartbeat Extender
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _lease_extender(
    subscriber: pubsub_v1.SubscriberClient,
    subscription_path: str,
    ack_id: str,
    ack_deadline_seconds: int = 60,
    interval_seconds: float = 20.0,
) -> Any:
    """Periodically extend the Pub/Sub message ack deadline while processing is in flight."""
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                subscriber.modify_ack_deadline(
                    request={
                        "subscription": subscription_path,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": ack_deadline_seconds,
                    }
                )
            except Exception as exc:
                logger.debug("Pub/Sub lease extension heartbeat skipped: %s", exc)

    thread = threading.Thread(target=_heartbeat, name="PubSubLeaseExtender", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5.0)


def setup_cloud_logging() -> None:
    """Initialize Google Cloud Logging handler if available."""
    from webhook_agent.constants import DEFAULT_PUBSUB_PROJECT

    try:
        import google.cloud.logging

        log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_name, logging.INFO)

        project_id = os.environ.get("PUBSUB_PROJECT", DEFAULT_PUBSUB_PROJECT)
        client = google.cloud.logging.Client(project=project_id)
        client.setup_logging(log_level=log_level)

        # Ensure third-party transport loggers remain quiet under cloud logging
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("google.auth").setLevel(logging.WARNING)
        logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)
        logging.getLogger("google_genai.models").setLevel(logging.ERROR)
        logging.getLogger("google_adk").setLevel(logging.ERROR)
        logging.getLogger("google.adk").setLevel(logging.ERROR)
        # Duplicate of the module-level clamp: drop ADK Runner's per-attempt
        # 'Root node failed' traceback (our orchestrator logs it once instead).
        logging.getLogger("google_adk.google.adk.runners").setLevel(logging.CRITICAL)
        logging.getLogger("google_adk.google.adk.workflow").setLevel(logging.CRITICAL)
        warnings.filterwarnings("ignore", category=UserWarning, module=r"google\.adk")

        logger.info("☁️ Google Cloud Logging initialized for project [%s]", project_id)
    except Exception as exc:
        logger.warning("Could not initialize Google Cloud Logging handler: %s", exc)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> int:
    from webhook_agent.constants import (
        DEFAULT_PUBSUB_DEAD_LETTER_TOPIC,
        DEFAULT_PUBSUB_PROJECT,
        DEFAULT_PUBSUB_SUBSCRIPTION,
    )

    setup_cloud_logging()
    os.environ.get("PUBSUB_PROJECT", DEFAULT_PUBSUB_PROJECT)
    subscription = os.environ.get("PUBSUB_SUBSCRIPTION", DEFAULT_PUBSUB_SUBSCRIPTION)
    dead_letter_topic = os.environ.get("PUBSUB_DEAD_LETTER_TOPIC", DEFAULT_PUBSUB_DEAD_LETTER_TOPIC)

    try:
        # We instantiate the processor here to validate environment variables early
        processor = WebhookProcessor()
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 3

    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    subscription_path = subscription
    keep_running = True

    def _signal_handler(signum: int, frame: Any) -> None:
        nonlocal keep_running
        if not keep_running:
            return  # Already shutting down; ignore repeated signals
        logger.info("🛑 Signal %s received, shutting down...", signum)
        keep_running = False

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    PROACTIVE_SWEEP_INTERVAL_SECONDS = 300  # 5 minutes
    last_proactive_sweep = 0.0
    active_sweep_thread: threading.Thread | None = None
    # Kill-switch: proactive sweeps disabled by default. Set
    # ENABLE_PROACTIVE_SWEEP=1 to re-enable the 5-minute ticker.
    proactive_sweep_enabled = os.environ.get("ENABLE_PROACTIVE_SWEEP", "0") in (
        "1",
        "true",
        "True",
    )

    REVIEW_RESUME_INTERVAL_SECONDS = int(os.environ.get("REVIEW_RESUME_INTERVAL_SECONDS", "60"))
    last_review_resume_sweep = 0.0
    review_resume_enabled = os.environ.get("ENABLE_REVIEW_RESUME", "0") in (
        "1",
        "true",
        "True",
    )

    logger.info("🚀 Starting sequential subscriber loop on %s", subscription_path)
    while keep_running:
        now = time.time()
        if (
            proactive_sweep_enabled
            and now - last_proactive_sweep >= PROACTIVE_SWEEP_INTERVAL_SECONDS
        ):
            last_proactive_sweep = now
            if active_sweep_thread is not None and active_sweep_thread.is_alive():
                logger.warning("Proactive background sweep skipped: previous sweep still alive")
            else:
                try:
                    import threading

                    from webhook_agent.review.proactive_service import ProactiveEvaluator

                    repos_env = os.environ.get(
                        "TARGET_REPOSITORIES",
                        "cgj8702-org/hannibal-hub,cgj8702-org/hannibal-hub-agents",
                    )
                    target_repos = [r.strip() for r in repos_env.split(",") if r.strip()]

                    def _run_sweeps(repos: list[str]) -> None:
                        for repo_name in repos:
                            try:
                                evaluator = ProactiveEvaluator(processor.gh, repo_name)
                                evaluator.evaluate_open_prs()
                            except GithubException as sweep_err:
                                if sweep_err.status != 401:
                                    logger.warning(
                                        "Proactive sweep error for %s: %s",
                                        repo_name,
                                        sweep_err,
                                    )
                                    continue
                                logger.warning(
                                    "Refreshing GitHub installation token after 401 for %s",
                                    repo_name,
                                )
                                processor.invalidate_github_client()
                                try:
                                    evaluator = ProactiveEvaluator(processor.gh, repo_name)
                                    evaluator.evaluate_open_prs()
                                except Exception as retry_err:
                                    logger.warning(
                                        "Proactive sweep retry failed for %s: %s",
                                        repo_name,
                                        retry_err,
                                    )
                            except Exception as sweep_err:
                                logger.warning(
                                    "Proactive sweep error for %s: %s",
                                    repo_name,
                                    sweep_err,
                                )

                    active_sweep_thread = threading.Thread(
                        target=_run_sweeps,
                        args=(target_repos,),
                        name="ProactiveSweepWorker",
                        daemon=True,
                    )
                    active_sweep_thread.start()
                except Exception as exc:
                    logger.warning("Proactive background sweep skipped/failed: %s", exc)

        # Periodic Review Checkpoint Resume Sweep (Every 60s when ENABLE_REVIEW_RESUME=1)
        if (
            review_resume_enabled
            and now - last_review_resume_sweep >= REVIEW_RESUME_INTERVAL_SECONDS
        ):
            last_review_resume_sweep = now
            try:
                from .state.review_checkpoint import (
                    build_resume_payload,
                    review_checkpoint_manager,
                )

                resumables = review_checkpoint_manager.list_resumable()
                if resumables:
                    logger.info("🔁 Found %d resumable review checkpoint(s)", len(resumables))
                    for checkpoint in resumables:
                        resume_payload = build_resume_payload(checkpoint)
                        processor.process_event(resume_payload)
            except Exception as resume_err:
                logger.warning("Review resume sweep error: %s", resume_err)

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
            # Extend Pub/Sub ack deadline periodically during long-running processing
            max_attempts = len(_RETRY_DELAYS) + 1
            with _lease_extender(subscriber, subscription_path, ack_id):
                for attempt in range(max_attempts):
                    try:
                        processor.process_event(payload)
                        break
                    except Exception as exc:
                        if attempt + 1 < max_attempts and _is_transient(exc):
                            base_delay = _RETRY_DELAYS[attempt]
                            jittered_delay = base_delay * random.uniform(  # noqa: S311 - pseudo-random jitter for network retry, non-cryptographic
                                0.8, 1.2
                            )
                            logger.warning(
                                "⚠️ Transient failure on attempt %d/%d for message %s: %s. Retrying in %.2fs...",
                                attempt + 1,
                                max_attempts,
                                str(message.message_id)[-4:],
                                exc,
                                jittered_delay,
                            )
                            if jittered_delay > 0:
                                time.sleep(jittered_delay)
                            continue
                        raise

            subscriber.acknowledge(request={"subscription": subscription_path, "ack_ids": [ack_id]})
            logger.debug("✅ Acked message: %s", str(message.message_id)[-4:])
        except Exception:
            logger.exception(
                "💥 Processing failed for message %s",
                str(message.message_id)[-4:],
            )
            if dead_letter_topic:
                if publish_dead_letter(publisher, dead_letter_topic, message.data):
                    subscriber.acknowledge(
                        request={"subscription": subscription_path, "ack_ids": [ack_id]}
                    )
                else:
                    logger.error(
                        "💥 Failed to publish message %s to dead-letter topic; leaving unacked",
                        str(message.message_id)[-4:],
                    )
            else:
                # Let Pub/Sub redeliver by not acking
                logger.debug("🔄 Not acking message to allow retry")

    return 0


if __name__ == "__main__":
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level_name, logging.INFO),
        format="[%(asctime)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    raise SystemExit(main())
