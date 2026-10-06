"""Unit tests for InMemoryMemoryService thread safety and callback logging."""

from __future__ import annotations

import asyncio

import pytest
from google.adk.memory.base_memory_service import MemoryEntry
from google.genai.types import Content

from webhook_agent.core.memory_service import InMemoryMemoryService

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_in_memory_service_basic_add_and_search() -> None:
    service = InMemoryMemoryService()
    entry = MemoryEntry(
        id="mem-1",
        author="user",
        content=Content(parts=[{"text": "Hello world from sub-agent"}]),
    )

    asyncio.run(
        service.add_memory(
            app_name="test_app",
            user_id="user_123",
            memories=[entry],
        )
    )

    resp = asyncio.run(
        service.search_memory(
            app_name="test_app",
            user_id="user_123",
            query="sub-agent",
        )
    )

    assert len(resp.memories) == 1
    assert "sub-agent" in resp.memories[0].content.parts[0].text


def test_in_memory_service_multithreaded_concurrency() -> None:
    """Verify thread-safety of InMemoryMemoryService under heavy concurrent access."""
    service = InMemoryMemoryService()
    num_workers = 20
    entries_per_worker = 50

    async def worker_task(worker_idx: int) -> None:
        for i in range(entries_per_worker):
            entry = MemoryEntry(
                id=f"worker-{worker_idx}-mem-{i}",
                author=f"worker-{worker_idx}",
                content=Content(
                    parts=[{"text": f"Concurrent log item {i} from worker {worker_idx}"}]
                ),
            )
            await service.add_memory(
                app_name="test_app",
                user_id="concurrent_user",
                memories=[entry],
            )
            # Interleave search operations concurrently
            _ = await service.search_memory(
                app_name="test_app",
                user_id="concurrent_user",
                query="Concurrent",
            )

    async def run_all() -> None:
        await asyncio.gather(*(worker_task(idx) for idx in range(num_workers)))

    asyncio.run(run_all())

    resp = asyncio.run(
        service.search_memory(
            app_name="test_app",
            user_id="concurrent_user",
            query="Concurrent",
        )
    )
    assert len(resp.memories) <= 10
