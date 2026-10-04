"""Background event loop and shared GenAI client helpers.

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from webhook_agent.logic.genai_provider import get_text_generation_provider
from webhook_agent.logic.rate_limiter import get_active_api_key

logger = logging.getLogger("webhook_agent.core.loop_helpers")

_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_LOOP_THREAD: threading.Thread | None = None
_BG_LOOP_LOCK = threading.RLock()
_GENAI_CLIENT: Any = None


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """Ensure process-wide background asyncio event loop is running."""
    global _BG_LOOP, _BG_LOOP_THREAD
    with _BG_LOOP_LOCK:
        if _BG_LOOP and _BG_LOOP.is_running():
            return _BG_LOOP

        loop = asyncio.new_event_loop()

        def _loop_worker() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except Exception:
                logger.exception("Background event loop crashed")
            finally:
                loop.close()

        thread = threading.Thread(target=_loop_worker, name="adk-bg-loop", daemon=True)
        thread.start()
        start_deadline = time.time() + 5.0
        while not loop.is_running() and time.time() < start_deadline:
            time.sleep(0.01)
        _BG_LOOP = loop
        _BG_LOOP_THREAD = thread
        return _BG_LOOP


def run_in_bg_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """Schedule coroutine on the background loop and wait for result.

    Guards against self-deadlock when called re-entrantly from within the background loop thread.
    """
    loop = _ensure_bg_loop()

    # Prevent thread self-deadlock if invoked from inside the background loop thread
    if threading.current_thread() == _BG_LOOP_THREAD:
        import nest_asyncio

        nest_asyncio.apply(loop)
        return loop.run_until_complete(coro)

    future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        # Wait for result; use a 600s timeout to allow complex multi-step reasoning
        # and rate-limiter pauses without prematurely failing the delivery.
        return future.result(timeout=600)
    except (TimeoutError, FutureTimeoutError):
        future.cancel()
        logger.error(
            "⏱️ Timed out waiting for coroutine in background loop (600s); cancelled background task"
        )
        raise
    except CancelledError:
        future.cancel()
        raise
    except Exception:
        # Re-raise after logging to make debugging easier in logs
        logger.exception("Error running coroutine in background loop")
        raise


def get_shared_genai_client() -> Any:
    """Return a process-wide cached google.genai Client. Returns None if no API key is configured."""
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT

    try:
        api_key = get_active_api_key()
    except Exception:
        api_key = None

    if not api_key:
        logger.debug("No active GenAI API key available to construct shared client")
        return None

    try:
        from google.genai import Client

        _GENAI_CLIENT = Client(api_key=api_key)
        logger.info("Shared GenAI client created and cached successfully")
        return _GENAI_CLIENT
    except Exception as exc:
        logger.exception("Failed to create shared GenAI client: %s", exc)
        return None


def get_shared_text_generation_provider() -> Any | None:
    """Return the shared provider adapter for the default generateContent path."""
    client = get_shared_genai_client()
    if client is None:
        return None
    return get_text_generation_provider(client)
