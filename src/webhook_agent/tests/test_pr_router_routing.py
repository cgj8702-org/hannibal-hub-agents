"""Routing tests for the `pr_router` dynamic workflow edges.

`pr_router` emits a route from `router_after_agent_callback` so docs-only PRs
reach `verdict_agent` without paying for the deep `code_auditor` pass. The
pipeline mirror below runs the real callback and the real edge topology with a
deterministic fake model, so no network access or rate limiter is involved.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import DEFAULT_ROUTE, START, Edge, Workflow
from google.genai import types

from webhook_agent.audit_schema import AuditVerdict
from webhook_agent.callbacks import ROUTE_CORE_BACKEND, ROUTE_DEV_DOCS, router_after_agent_callback

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

_APP_NAME = "pr_router_routing_test"
_VERDICT_JSON = (
    '{"verdict":"APPROVE","confidence":4.5,"pr_type":"dev_docs","summary":"docs only","risks":[]}'
)


class _FakeLlm(BaseLlm):
    """Deterministic stand-in for a Gemini model — no network, no rate limiting."""

    text: str = "ok"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=self.text)]),
            partial=False,
        )


def _build_pipeline(router_text: str, *, with_router_callback: bool = True) -> Workflow:
    """Mirror of the WebhookAgent graph with faked models."""
    router = LlmAgent(
        name="pr_router",
        model=_FakeLlm(model="fake-router", text=router_text),
        instruction="Classify the PR scope.",
        output_key="pr_scope",
        after_agent_callback=(router_after_agent_callback if with_router_callback else None),
    )
    auditor = LlmAgent(
        name="code_auditor",
        model=_FakeLlm(model="fake-auditor", text="audit findings"),
        instruction="Audit the PR.",
        output_key="code_review_analysis",
    )
    verdict = LlmAgent(
        name="verdict_agent",
        model=_FakeLlm(model="fake-verdict", text=_VERDICT_JSON),
        instruction="Scope: {pr_scope?}\nAnalysis: {code_review_analysis?}",
        output_schema=AuditVerdict,
        output_key="audit_verdict",
    )
    return Workflow(
        name=_APP_NAME,
        edges=[
            (START, router),
            Edge(from_node=router, to_node=verdict, route=ROUTE_DEV_DOCS),
            Edge(from_node=router, to_node=auditor, route=DEFAULT_ROUTE),
            (auditor, verdict),
        ],
    )


async def _run_pipeline(
    router_text: str, *, with_router_callback: bool = True
) -> tuple[list[str], dict]:
    workflow = _build_pipeline(router_text, with_router_callback=with_router_callback)
    session_service = InMemorySessionService()
    runner = Runner(
        app=App(name=_APP_NAME, root_agent=workflow),
        session_service=session_service,
    )
    session = await session_service.create_session(app_name=_APP_NAME, user_id="tester")

    authors: list[str] = []
    async for event in runner.run_async(
        user_id="tester",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="PR diff")]),
    ):
        author = getattr(event, "author", None)
        if author:
            authors.append(author)

    final_session = await session_service.get_session(
        app_name=_APP_NAME, user_id="tester", session_id=session.id
    )
    return authors, (final_session.state if final_session else {})


@pytest.mark.anyio
async def test_dev_docs_route_skips_code_auditor() -> None:
    authors, state = await _run_pipeline("dev_docs")

    assert "code_auditor" not in authors
    assert "verdict_agent" in authors
    assert state.get("audit_verdict") is not None
    assert state.get("pr_scope_route") == ROUTE_DEV_DOCS


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("router_text", "expected_route"),
    [
        ("minor_fix", "minor_fix"),
        ("core_backend", ROUTE_CORE_BACKEND),
        ("totally unclear scope", ROUTE_CORE_BACKEND),
    ],
)
async def test_audited_routes_still_run_code_auditor(router_text: str, expected_route: str) -> None:
    authors, state = await _run_pipeline(router_text)

    assert "code_auditor" in authors
    assert "verdict_agent" in authors
    assert state.get("audit_verdict") is not None
    assert state.get("pr_scope_route") == expected_route


@pytest.mark.anyio
async def test_missing_route_falls_back_to_full_audit() -> None:
    """A router that emits no route must not silently skip the audit."""
    authors, state = await _run_pipeline("dev_docs", with_router_callback=False)

    assert "code_auditor" in authors
    assert state.get("audit_verdict") is not None
    assert state.get("pr_scope_route") is None


def test_webhook_agent_wires_router_routes_and_default_fallthrough() -> None:
    from webhook_agent.webhook_agent import WebhookAgent

    workflow = WebhookAgent(dry_run=True)._agent
    routes = {(edge.from_node.name, edge.to_node.name): edge.route for edge in workflow.graph.edges}

    assert routes[(START.name, "pr_router")] is None
    assert routes[("pr_router", "verdict_agent")] == ROUTE_DEV_DOCS
    assert routes[("pr_router", "code_auditor")] == DEFAULT_ROUTE
    assert routes[("code_auditor", "verdict_agent")] is None


def test_verdict_instruction_tolerates_missing_audit_analysis() -> None:
    """Skipping `code_auditor` must not break the verdict instruction template."""
    from webhook_agent.webhook_agent import WebhookAgent

    agent = WebhookAgent(dry_run=True)

    assert agent._pr_router.after_agent_callback is router_after_agent_callback
    assert "{code_review_analysis?}" in agent._verdict_agent.instruction
    assert "{pr_scope?}" in agent._verdict_agent.instruction
