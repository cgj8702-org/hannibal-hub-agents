"""Unit tests for BoundedSessionService and session isolation."""

from __future__ import annotations

import pytest
from google.adk.events import Event
from google.adk.events.event import EventActions

from webhook_agent.core.session_service import BoundedSessionService

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.anyio
async def test_bounded_session_service_default_capacity(monkeypatch):
    monkeypatch.delenv("MAX_IN_MEMORY_SESSIONS", raising=False)
    service = BoundedSessionService()
    assert service.max_sessions == 100

    service_custom = BoundedSessionService(max_sessions=5)
    assert service_custom.max_sessions == 5

    monkeypatch.setenv("MAX_IN_MEMORY_SESSIONS", "42")
    service_env = BoundedSessionService()
    assert service_env.max_sessions == 42


@pytest.mark.anyio
async def test_bounded_session_service_lru_eviction():
    service = BoundedSessionService(max_sessions=3)
    app = "test_app"
    user = "user_1"

    # Create 3 sessions: s1, s2, s3
    s1 = await service.create_session(app_name=app, user_id=user, session_id="s1")
    s2 = await service.create_session(app_name=app, user_id=user, session_id="s2")
    s3 = await service.create_session(app_name=app, user_id=user, session_id="s3")

    assert await service.get_session(app_name=app, user_id=user, session_id="s1") is not None
    assert await service.get_session(app_name=app, user_id=user, session_id="s2") is not None
    assert await service.get_session(app_name=app, user_id=user, session_id="s3") is not None

    # Access s1, making s2 the oldest (LRU order: s3, s1)
    await service.get_session(app_name=app, user_id=user, session_id="s1")

    # Create s4 -> should evict s2 (since s1 was refreshed)
    s4 = await service.create_session(app_name=app, user_id=user, session_id="s4")

    assert await service.get_session(app_name=app, user_id=user, session_id="s2") is None
    assert await service.get_session(app_name=app, user_id=user, session_id="s1") is not None
    assert await service.get_session(app_name=app, user_id=user, session_id="s3") is not None
    assert await service.get_session(app_name=app, user_id=user, session_id="s4") is not None


@pytest.mark.anyio
async def test_bounded_session_service_append_event_touches_lru():
    service = BoundedSessionService(max_sessions=2)
    app = "test_app"
    user = "user_1"

    s1 = await service.create_session(app_name=app, user_id=user, session_id="s1")
    s2 = await service.create_session(app_name=app, user_id=user, session_id="s2")

    # Append event to s1, refreshing its LRU priority over s2
    await service.append_event(
        s1,
        Event(
            invocation_id="inv-1",
            author="tester",
            actions=EventActions(state_delta={"foo": "bar"}),
        ),
    )

    # Creating s3 should evict s2, not s1
    await service.create_session(app_name=app, user_id=user, session_id="s3")

    assert await service.get_session(app_name=app, user_id=user, session_id="s2") is None
    assert await service.get_session(app_name=app, user_id=user, session_id="s1") is not None
    assert await service.get_session(app_name=app, user_id=user, session_id="s3") is not None


@pytest.mark.anyio
async def test_bounded_session_service_delete_session():
    service = BoundedSessionService(max_sessions=2)
    app = "test_app"
    user = "user_1"

    await service.create_session(app_name=app, user_id=user, session_id="s1")
    await service.create_session(app_name=app, user_id=user, session_id="s2")

    await service.delete_session(app_name=app, user_id=user, session_id="s1")
    assert await service.get_session(app_name=app, user_id=user, session_id="s1") is None
    assert (app, user, "s1") not in service._lru_order


@pytest.mark.anyio
async def test_session_isolation_between_audit_and_conversational():
    service = BoundedSessionService()
    audit_app = "hannibal-hub-agents"
    conv_app = "hannibal-hub-agents_conversational"
    user = "github_pr_user"
    session_id = "repo/owner/123"

    # Simulate formal audit creating a session with heavy AST/dossier state
    audit_session = await service.create_session(
        app_name=audit_app,
        user_id=user,
        session_id=session_id,
        state={
            "audit_dossier": "15,000 tokens of precompiled AST symbol graphs and diffs",
            "audit_verdict": "REQUEST_CHANGES",
        },
    )

    # Conversational agent accesses session for the same PR/user
    conv_session = await service.get_session(
        app_name=conv_app,
        user_id=user,
        session_id=session_id,
    )
    # Conversational session must not exist yet (completely isolated from audit session)
    assert conv_session is None

    # Conversational agent creates its own session
    conv_session = await service.create_session(
        app_name=conv_app,
        user_id=user,
        session_id=session_id,
        state={"conversation_topic": "clarifying comment on line 42"},
    )

    # Verify both sessions exist independently without data bleed
    read_audit = await service.get_session(
        app_name=audit_app,
        user_id=user,
        session_id=session_id,
    )
    read_conv = await service.get_session(
        app_name=conv_app,
        user_id=user,
        session_id=session_id,
    )

    assert read_audit is not None
    assert "audit_dossier" in read_audit.state
    assert "conversation_topic" not in read_audit.state

    assert read_conv is not None
    assert "conversation_topic" in read_conv.state
    assert "audit_dossier" not in read_conv.state
