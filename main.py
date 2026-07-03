import logging
import multiprocessing
import signal
import sys

import uvicorn

# Configure logging for the entry point
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("main")


def run_receiver():
    """Launch the FastAPI webhook receiver."""
    logger.info("🚀 Starting Webhook Receiver...")
    uvicorn.run(
        "src.webhook_agent.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=True,  # Set to False in production
    )


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
    Starts both the receiver and the worker as separate processes.
    """
    logger.info("🚀 Starting hannibal-hub-agents distributed architecture...")

    # Create processes for the receiver and worker
    receiver_proc = multiprocessing.Process(target=run_receiver, name="Receiver")
    worker_proc = multiprocessing.Process(target=run_worker, name="Worker")

    # Start processes
    receiver_proc.start()
    worker_proc.start()

    def signal_handler(sig, frame):
        logger.info(f"🛑 Received signal {sig}, shutting down services...")
        receiver_proc.terminate()
        worker_proc.terminate()
        receiver_proc.join()
        worker_proc.join()
        logger.info("✅ All services shut down.")
        sys.exit(0)

    # Handle graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep the main process alive while children are running
    receiver_proc.join()
    worker_proc.join()


if __name__ == "__main__":
    main()
