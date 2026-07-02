import uvicorn
import logging

# Configure logging for the entry point
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    """
    Launch the unified webhook agent server.
    This starts the FastAPI app, which in turn starts the background
    event processor and the Cloudflare tunnel (if configured).
    """
    print("🚀 Starting hannibal-hub-agents unified server...")
    uvicorn.run(
        "src.webhook_agent.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=True,  # Set to False in production
    )


if __name__ == "__main__":
    main()
