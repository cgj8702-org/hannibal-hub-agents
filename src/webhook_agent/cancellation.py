"""PR Closed Cancellation Registry & Exception.

Tracks closed PRs in memory across async execution turns to immediately
short-circuit active agents working on closed or merged pull requests.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("webhook_agent.cancellation")


class AbortAgentExecution(Exception):
    """Raised when an agent execution must be short-circuited immediately (e.g., closed PR)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PRClosedRegistry:
    """Thread-safe registry of closed PRs to signal active worker tasks to abort."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._closed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def _key(self, repo_full_name: str, pr_number: int) -> str:
        return f"{repo_full_name.lower()}#{pr_number}"

    def mark_closed(self, repo_full_name: str, pr_number: int) -> None:
        key = self._key(repo_full_name, pr_number)
        with self._lock:
            self._closed[key] = time.time()
        logger.info("🔒 Registered PR %s as CLOSED for short-circuiting", key)

    def is_closed(self, repo_full_name: str, pr_number: int) -> bool:
        key = self._key(repo_full_name, pr_number)
        with self._lock:
            if key not in self._closed:
                return False
            timestamp = self._closed[key]
            if time.time() - timestamp > self._ttl:
                del self._closed[key]
                return False
            return True


# Global singleton instance
pr_closed_registry = PRClosedRegistry()
