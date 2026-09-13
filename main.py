from __future__ import annotations

import logging
import multiprocessing
import signal
import sys
from pathlib import Path
from types import FrameType

# Ensure src/ is on sys.path for direct entry point execution
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Configure logging for the entry point
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("main")


def run_worker() -> None:
    """Launch the Pub/Sub event processor worker."""
    logger.info("🚀 Starting Webhook Processor Worker...")
    from webhook_agent import worker

    try:
        sys.exit(worker.main())
    except SystemExit as e:
        sys.exit(e.code)


def main() -> None:
    """
    Orchestrate the hannibal-hub-agents services.
    Starts the worker as a separate process.
    """
    logger.info("🚀 Starting hannibal-hub-agents distributed architecture...")

    # Create process for the worker
    worker_proc = multiprocessing.Process(target=run_worker, name="Worker")

    # Start process
    worker_proc.start()

    def signal_handler(sig: int, frame: FrameType | None) -> None:
        logger.info("🛑 Received signal %d, shutting down services...", sig)
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
