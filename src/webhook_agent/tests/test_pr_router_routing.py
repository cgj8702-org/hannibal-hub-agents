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
from google.adk.events import Event, EventActions
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
    router_text: str,
    *,
    with_router_callback: bool = True,
    deterministic_pr_scope: str | None = None,
) -> tuple[list[str], dict]:
    workflow = _build_pipeline(router_text, with_router_callback=with_router_callback)
    session_service = InMemorySessionService()
    runner = Runner(
        app=App(name=_APP_NAME, root_agent=workflow),
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=_APP_NAME,
        user_id="tester",
        state={"deterministic_pr_scope": deterministic_pr_scope}
        if deterministic_pr_scope is not None
        else None,
    )

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
    authors, state = await _run_pipeline("dev_docs", deterministic_pr_scope=ROUTE_DEV_DOCS)

    assert "code_auditor" not in authors
    assert "verdict_agent" in authors
    assert state.get("audit_verdict") is not None
    assert state.get("pr_scope_route") == ROUTE_DEV_DOCS


@pytest.mark.anyio
async def test_deterministic_core_backend_overrides_model_dev_docs() -> None:
    authors, state = await _run_pipeline("dev_docs", deterministic_pr_scope=ROUTE_CORE_BACKEND)

    assert "code_auditor" in authors
    assert "verdict_agent" in authors
    assert state.get("pr_scope_route") == ROUTE_CORE_BACKEND


@pytest.mark.anyio
async def test_scope_gate_refreshes_on_existing_session() -> None:
    session_service = InMemorySessionService()
    runner = Runner(
        app=App(name=_APP_NAME, root_agent=_build_pipeline("dev_docs")),
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=_APP_NAME,
        user_id="tester",
        session_id="repo/42",
        state={"deterministic_pr_scope": ROUTE_CORE_BACKEND},
    )

    first_authors = [
        event.author
        async for event in runner.run_async(
            user_id="tester",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="first")]),
        )
        if event.author
    ]
    assert "code_auditor" in first_authors

    await session_service.append_event(
        session,
        Event(
            invocation_id="second-turn",
            actions=EventActions(state_delta={"deterministic_pr_scope": ROUTE_DEV_DOCS}),
        ),
    )
    second_authors = [
        event.author
        async for event in runner.run_async(
            user_id="tester",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="second")]),
        )
        if event.author
    ]

    assert "code_auditor" not in second_authors
    final_session = await session_service.get_session(
        app_name=_APP_NAME, user_id="tester", session_id=session.id
    )
    assert final_session is not None
    assert final_session.state["pr_scope_route"] == ROUTE_DEV_DOCS


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


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.parametrize(
    ("paths", "expected_route"),
    [
        (["README.md", "docs/guide.rst"], "dev_docs"),
        (["README.md", "src/main.py"], "core_backend"),
        (["docs/guide.md", "dev/tool.py"], "core_backend"),
        (["docs/guide.md", "scripts/deploy.sh"], "core_backend"),
        (["docs/guide.md", ".githooks/pre-commit"], "core_backend"),
        (["docs/guide.md", ".github/workflows/ci.yml"], "core_backend"),
        (["docs/guide.md", "tests/unit/test_main.py"], "core_backend"),
        (["docs/guide.md", "pyproject.toml"], "core_backend"),
        (["docs/guide.md", "uv.lock"], "core_backend"),
        (["README.md", "unknown"], "core_backend"),
        ([], "core_backend"),
    ],
)
def test_deterministic_pr_scope_fails_closed(paths: list[str], expected_route: str) -> None:
    from webhook_agent.logic.scope_router import deterministic_pr_scope

    assert deterministic_pr_scope(paths) == expected_route


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_deterministic_pr_scope_extracts_paths_from_custom_diff_format() -> None:
    from webhook_agent.logic.scope_router import deterministic_pr_scope_from_diff

    diff = """File: README.md (modified)
Patch:
-old
+new
----------------------------------------
File: src/main.py (modified)
Patch:
@@ -1 +1 @@
-old
+new
----------------------------------------
"""

    assert deterministic_pr_scope_from_diff(diff) == "core_backend"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_build_user_message_includes_deterministic_file_inventory() -> None:
    from webhook_agent.logic.scope_router import build_deterministic_scope_context
    from webhook_agent.webhook_agent import WebhookAgent

    context = build_deterministic_scope_context("File: README.md (modified)\nPatch:\n-old\n+new\n")
    agent = WebhookAgent(dry_run=True)
    message = agent._build_user_message(
        {
            "canonical": "pull_request.opened",
            "raw_payload": {
                "pull_request": {"number": 1, "title": "Update docs"},
                "pr_diff": context.diff,
            },
        }
    )

    text = message.parts[0].text or ""
    assert "Deterministic scope safety gate: dev_docs" in text
    assert "Changed file inventory: README.md" in text


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
