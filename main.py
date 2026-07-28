import logging
import multiprocessing
import os
import signal
import sys

# Configure logging for the entry point
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("main")


def setup_cloud_logging():
    """Initialize Google Cloud Logging handler if available."""
    try:
        import google.cloud.logging

        project_id = os.environ.get("PUBSUB_PROJECT", "cgj8702-webhook-agent")
        client = google.cloud.logging.Client(project=project_id)
        client.setup_logging()
        logger.info(
            f"☁️ Google Cloud Logging initialized for project [{project_id}]"
        )
    except Exception as e:
        logger.warning(f"Could not setup Google Cloud Logging handler: {e}")



def run_worker():
    """Launch the Pub/Sub event processor worker."""
    logger.info("🚀 Starting Webhook Processor Worker...")
    # We use uv run if available, or just python
    # Since we are in a uv environment, we can call the module directly
    from src.webhook_agent import worker

    try:
        sys.exit(worker.main())
    except SystemExit as e:
        sys.exit(e.code)


def main():
    """
    Orchestrate the hannibal-hub-agents services.
    Starts the worker as a separate process.
    """
    setup_cloud_logging()
    logger.info("🚀 Starting hannibal-hub-agents distributed architecture...")

    # Create process for the worker
    worker_proc = multiprocessing.Process(target=run_worker, name="Worker")

    # Start process
    worker_proc.start()

    def signal_handler(sig, frame):
        logger.info(f"🛑 Received signal {sig}, shutting down services...")
        worker_proc.terminate()
        worker_proc.join()
        logger.info("✅ All services shut down.")
        sys.exit(0)

    # Handle graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep the main process alive while children are running
    worker_proc.join()


if __name__ == "__main__":
    main()
