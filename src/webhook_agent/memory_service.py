"""Transitional backwards-compatibility shim for memory_service.

Relocated to webhook_agent.core.memory_service in Phase 6a modularization.
"""

from __future__ import annotations

from webhook_agent.core.memory_service import InMemoryMemoryService, logger

__all__ = ["InMemoryMemoryService", "logger"]
