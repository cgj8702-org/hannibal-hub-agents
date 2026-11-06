"""Bounded in-memory session service with LRU eviction for ADK agents."""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any

from google.adk.events import Event
from google.adk.sessions import InMemorySessionService, Session
from google.adk.sessions.base_session_service import GetSessionConfig

logger = logging.getLogger("webhook_agent.core.session_service")

DEFAULT_MAX_SESSIONS = 100


class BoundedSessionService(InMemorySessionService):
    """In-memory session service with bounded capacity and LRU eviction.

    Prevents unbounded memory growth in long-running worker processes by evicting
    the least-recently-accessed sessions once the session limit is exceeded.
    """

    def __init__(self, max_sessions: int | None = None) -> None:
        super().__init__()
        if max_sessions is not None:
            self.max_sessions = max(1, max_sessions)
        else:
            try:
                env_limit = int(os.environ.get("MAX_IN_MEMORY_SESSIONS", str(DEFAULT_MAX_SESSIONS)))
                self.max_sessions = max(1, env_limit)
            except ValueError:
                self.max_sessions = DEFAULT_MAX_SESSIONS

        # Key: (app_name, user_id, session_id) -> None (tracks access order)
        self._lru_order: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def _touch(self, app_name: str, user_id: str, session_id: str) -> None:
        """Mark a session key as recently used."""
        key = (app_name, user_id, session_id)
        self._lru_order[key] = None
        self._lru_order.move_to_end(key)

    def _evict_if_needed(self) -> None:
        """Evict oldest sessions until current count is within max_sessions."""
        while len(self._lru_order) > self.max_sessions:
            (old_app, old_user, old_sid), _ = self._lru_order.popitem(last=False)
            if old_app in self.sessions and old_user in self.sessions[old_app]:
                self.sessions[old_app][old_user].pop(old_sid, None)
                if not self.sessions[old_app][old_user]:
                    self.sessions[old_app].pop(old_user, None)
                    # Clean up user_state for that user in old_app if no sessions remain
                    if old_app in self.user_state:
                        self.user_state[old_app].pop(old_user, None)
                if not self.sessions[old_app]:
                    self.sessions.pop(old_app, None)
                    if old_app in self.user_state and not self.user_state[old_app]:
                        self.user_state.pop(old_app, None)
            logger.debug(
                "Evicted LRU session %s (app: %s, user: %s)",
                old_sid,
                old_app,
                old_user,
            )

    def _create_session_impl(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        session = super()._create_session_impl(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        self._touch(app_name, user_id, session.id)
        self._evict_if_needed()
        return session

    def _get_session_impl(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        session = super()._get_session_impl(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            config=config,
        )
        if session is not None:
            self._touch(app_name, user_id, session_id.strip() if session_id else session_id)
        return session

    def _delete_session_impl(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        sid = session_id.strip() if session_id else session_id
        super()._delete_session_impl(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        self._lru_order.pop((app_name, user_id, sid), None)

    async def append_event(self, session: Session, event: Event) -> Event:
        res = await super().append_event(session=session, event=event)
        self._touch(session.app_name, session.user_id, session.id)
        return res
