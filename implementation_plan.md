Plan to implement PR #18 fixes:

1. **Unified Async Architecture (Critical)**
   - **Refactor `src/webhook_agent/app.py`**: Replace `publish_webhook_message` (Pub/Sub) with a push to a global `asyncio.Queue`.
   - **Refactor `src/webhook_agent/worker.py`**: Convert the worker to an `async` function (`async def worker_loop()`) that consumes from the `asyncio.Queue`.
   - **Refactor `main.py`**: 
     - Remove `multiprocessing` for the worker.
     - Use FastAPI's `lifespan` event in `app.py` to start the `worker_loop` as a background task using `asyncio.create_task`.
     - Simplify `main.py` to only launch the FastAPI server.
   - **Cleanup**: Remove `src/webhook_agent/enqueue.py` as it will no longer be needed for the unified in-memory architecture.

2. **Dynamic Bot Identity (Minor)**
   - **Update `src/webhook_agent/processor.py`**: Replace the hardcoded `BOT_LOGIN` with `os.environ.get('BOT_LOGIN', 'hannibal-hub-agents[bot]')`.

3. **Worker Reliability (Minor)**
   - **Implement Supervisor Pattern**: In the `lifespan` handler, wrap the worker task in a supervisor loop that logs failures and restarts the worker if it crashes.

4. **Consistency & Polishing (Nitpick)**
   - **Standardize Environment Access**: Ensure all environment variable lookups in `app.py` use `os.environ.get`.

5. **Verification**
   - Run `scripts/ruff-all.sh` to ensure linting compliance.
   - Verify the unified server starts correctly.
   - Test end-to-end flow from webhook delivery to processing using a mock payload.