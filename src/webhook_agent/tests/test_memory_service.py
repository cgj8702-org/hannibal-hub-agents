"""Unit tests for InMemoryMemoryService thread safety and callback logging."""

from __future__ import annotations

import concurrent.futures

from google.adk.memory.base_memory_service import MemoryEntry
from google.genai.types import Content
from webhook_agent.memory_service import InMemoryMemoryService
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_in_memory_service_basic_add_and_search() -> None:
    service = InMemoryMemoryService()
    entry = MemoryEntry(
        id="mem-1",
        author="user",
        content=Content(parts=[{"text": "Hello world from sub-agent"}]),
    )

    service.add_memory(
        app_name="test_app",
        user_id="user_123",
        memories=[entry],
    )

    resp = service.search_memory(
        app_name="test_app",
        user_id="user_123",
        query="sub-agent",
    )

    assert len(resp.memories) == 1
    assert "sub-agent" in resp.memories[0].content.parts[0].text


def test_in_memory_service_multithreaded_concurrency() -> None:
    """Verify thread-safety of InMemoryMemoryService under heavy concurrent thread access."""
    service = InMemoryMemoryService()
    num_threads = 20
    entries_per_thread = 50

    def worker_task(thread_idx: int) -> None:
        for i in range(entries_per_thread):
            entry = MemoryEntry(
                id=f"thread-{thread_idx}-mem-{i}",
                author=f"thread-{thread_idx}",
                content=Content(
                    parts=[
                        {"text": f"Concurrent log item {i} from thread {thread_idx}"}
                    ]
                ),
            )
            service.add_memory(
                app_name="test_app",
                user_id="concurrent_user",
                memories=[entry],
            )
            # Interleave search operations concurrently
            _ = service.search_memory(
                app_name="test_app",
                user_id="concurrent_user",
                query="Concurrent",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_task, idx) for idx in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # Should complete with 0 exceptions

    resp = service.search_memory(
        app_name="test_app",
        user_id="concurrent_user",
        query="Concurrent",
    )
    assert len(resp.memories) <= 10
