"""ChromaDB-backed memory service for ADK.

Implements BaseMemoryService using ChromaDB for persistent, searchable
conversation memory across restarts. Each memory entry is stored as a
ChromaDB document with embeddings for semantic search.

The service stores:
- Conversation turns as MemoryEntry objects
- Embeddings via sentence-transformers for semantic retrieval
- Metadata (app_name, user_id, author, timestamp) for filtering
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Sequence

import chromadb
from chromadb.config import Settings as ChromaSettings
from google.adk.events.event import Event
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    MemoryEntry,
    SearchMemoryResponse,
)
from google.adk.sessions.session import Session
from google.genai.types import Content

logger = logging.getLogger("chroma_memory")

# Default collection name for webhook agent memories
DEFAULT_COLLECTION = "webhook_agent_memories"


class ChromaDBMemoryService(BaseMemoryService):
    """ChromaDB-backed memory service for persistent agent memory.

    Stores conversation memories as ChromaDB documents with embeddings
    for semantic search. Supports filtering by app_name and user_id.
    """

    def __init__(
        self,
        collection_name: str = DEFAULT_COLLECTION,
        persist_directory: str = ".chroma_memory",
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        """Initialize the ChromaDB memory service.

        Args:
            collection_name: Name of the ChromaDB collection to use.
            persist_directory: Directory for persistent ChromaDB storage.
            embedding_model: Sentence-transformers model for embeddings.
        """
        self._collection_name = collection_name
        self._persist_directory = persist_directory
        self._embedding_model_name = embedding_model
        self._embedding_fn: Any = None
        self._client: chromadb.ClientAPI | None = None
        self._collection: chromadb.Collection | None = None

    def _lazy_init(self) -> None:
        """Initialize ChromaDB client and collection on first use."""
        if self._client is not None:
            return

        try:
            from sentence_transformers import SentenceTransformer

            self._embedding_fn = SentenceTransformer(self._embedding_model_name)
        except ImportError:
            logger.warning(
                "sentence-transformers not available, falling back to "
                "identity embeddings (semantic search disabled)"
            )
            self._embedding_fn = None

        self._client = chromadb.PersistentClient(
            path=self._persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        try:
            self._collection = self._client.get_collection(self._collection_name)
            logger.debug(
                "Reopened existing ChromaDB collection: %s",
                self._collection_name,
            )
        except ValueError:
            self._collection = self._client.create_collection(
                self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "Created ChromaDB collection: %s",
                self._collection_name,
            )

    def _compute_embedding(self, text: str) -> list[float]:
        """Compute embedding for a text string.

        Falls back to a zero vector if sentence-transformers is unavailable.
        """
        if self._embedding_fn is not None:
            return self._embedding_fn.encode(text).tolist()
        return [0.0] * 384  # fallback zero vector

    def _content_to_text(self, content: Content) -> str:
        """Extract text from a Content object for embedding."""
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
        """Store memory entries in ChromaDB."""
        self._lazy_init()
        if self._collection is None:
            logger.error("ChromaDB collection not initialized")
            return

        for entry in memories:
            text = self._content_to_text(entry.content)
            if not text:
                continue

            mem_id = entry.id or str(uuid.uuid4())
            embedding = self._compute_embedding(text)

            metadata: dict[str, Any] = {
                "app_name": app_name,
                "user_id": user_id,
                "author": entry.author or "unknown",
                "timestamp": entry.timestamp or "",
            }
            if entry.custom_metadata:
                metadata.update({k: str(v) for k, v in entry.custom_metadata.items()})
            if custom_metadata:
                metadata.update({k: str(v) for k, v in custom_metadata.items()})

            self._collection.add(
                ids=[mem_id],
                embeddings=[embedding],
                documents=[text],
                metadatas=[metadata],
            )

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
        """Search memories by semantic similarity to the query."""
        self._lazy_init()
        if self._collection is None:
            return SearchMemoryResponse(memories=[])

        query_embedding = self._compute_embedding(query)

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=10,
            where={
                "$and": [
                    {"app_name": {"$eq": app_name}},
                    {"user_id": {"$eq": user_id}},
                ]
            },
        )

        memories: list[MemoryEntry] = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                document = results["documents"][0][i] if results["documents"] else ""
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}

                content = Content(parts=[{"text": document}])
                memories.append(
                    MemoryEntry(
                        id=doc_id,
                        author=metadata.get("author", "unknown"),
                        content=content,
                        timestamp=metadata.get("timestamp", ""),
                        custom_metadata={
                            k: v
                            for k, v in metadata.items()
                            if k not in ("app_name", "user_id", "author", "timestamp")
                        },
                    )
                )

        return SearchMemoryResponse(memories=memories)
