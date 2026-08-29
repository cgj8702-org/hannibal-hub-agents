"""In-memory memory service for ADK.

Implements BaseMemoryService using plain Python dicts for lightweight,
non-persistent conversation memory. This replaces the previous ChromaDB-backed
implementation to reduce dependencies and deployment size.

The service stores:
- Conversation turns as MemoryEntry objects
- Metadata (app_name, user_id, author, timestamp) for filtering
- No embeddings, no vector search, no disk persistence
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Any, Mapping, Sequence

from google.adk.events.event import Event
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    MemoryEntry,
    SearchMemoryResponse,
)
from google.adk.sessions.session import Session
from google.genai.types import Content

logger = logging.getLogger("webhook_agent.memory")


class InMemoryMemoryService(BaseMemoryService):
    """In-memory memory service for lightweight agent memory.

    Stores conversation memories as plain Python dicts. Supports filtering
    by app_name and user_id. No persistence across restarts.
    """

    def __init__(self) -> None:
        """Initialize the in-memory memory service."""
        self._memories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # Key format: "{app_name}:{user_id}"

    def _key(self, app_name: str, user_id: str) -> str:
        return f"{app_name}:{user_id}"

    def _content_to_text(self, content: Content) -> str:
        """Extract text from a Content object."""
        parts = []
        if content.parts:
            for part in content.parts:
                if hasattr(part, "text") and part.text:
                    parts.append(part.text)
        return " ".join(parts) if parts else ""

    def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Store memory entries in memory."""
        key = self._key(app_name, user_id)
        for entry in memories:
            text = self._content_to_text(entry.content)
            if not text:
                continue

            mem_id = entry.id or str(uuid.uuid4())
            metadata: dict[str, Any] = {
                "app_name": app_name,
                "user_id": user_id,
                "author": entry.author or "unknown",
                "timestamp": entry.timestamp or "",
                "text": text,
            }
            if entry.custom_metadata:
                metadata.update({k: str(v) for k, v in entry.custom_metadata.items()})
            if custom_metadata:
                metadata.update({k: str(v) for k, v in custom_metadata.items()})

            self._memories[key].append({"id": mem_id, "metadata": metadata})

        logger.debug(
            "Stored %d memory entries for user %s in app %s",
            len(memories),
            user_id,
            app_name,
        )

    def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Convert events to memory entries and store them."""
        memories: list[MemoryEntry] = []
        for event in events:
            author = event.author or "unknown"
            content = event.content
            if content is None:
                continue

            metadata: dict[str, Any] = {}
            if session_id:
                metadata["session_id"] = session_id
            if custom_metadata:
                metadata.update({k: str(v) for k, v in custom_metadata.items()})

            memories.append(
                MemoryEntry(
                    id=str(uuid.uuid4()),
                    author=author,
                    content=content,
                    timestamp=str(event.timestamp) if event.timestamp else "",
                    custom_metadata=metadata,
                )
            )

        if memories:
            self.add_memory(
                app_name=app_name,
                user_id=user_id,
                memories=memories,
                custom_metadata=custom_metadata,
            )

    def add_session_to_memory(self, session: Session) -> None:
        """Store all events from a session as memories."""
        self.add_events_to_memory(
            app_name=session.app_name,
            user_id=session.user_id,
            events=session.events,
            session_id=session.id,
        )

    def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Search memories by keyword matching on the query text.

        Since this is a lightweight in-memory implementation without embeddings,
        search is performed by simple substring matching against stored text.
        Returns up to 10 most recent matching entries.
        """
        key = self._key(app_name, user_id)
        entries = self._memories.get(key, [])

        # Filter by keyword match (case-insensitive)
        query_lower = query.lower()
        matched = [
            e for e in entries if query_lower in e["metadata"].get("text", "").lower()
        ]

        # Return up to 10 most recent matches
        matched = matched[-10:]

        memories: list[MemoryEntry] = []
        for entry in matched:
            metadata = entry["metadata"]
            content = Content(parts=[{"text": metadata.get("text", "")}])
            memories.append(
                MemoryEntry(
                    id=entry["id"],
                    author=metadata.get("author", "unknown"),
                    content=content,
                    timestamp=metadata.get("timestamp", ""),
                    custom_metadata={
                        k: v
                        for k, v in metadata.items()
                        if k
                        not in ("app_name", "user_id", "author", "timestamp", "text")
                    },
                )
            )

        return SearchMemoryResponse(memories=memories)
