"""Unit tests for High-Trust Output & Scope-Aware Template System."""

from __future__ import annotations

import pytest
from webhook_agent.audit_schema import AuditVerdict, RiskItem
from webhook_agent.comment_poster import render_review_markdown

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_dev_docs_minimal_scope_rendering() -> None:
    verdict = AuditVerdict(
        verdict="APPROVE",
        confidence=5.0,
        pr_type="dev_docs",
        summary="Clean README documentation update.",
        risks=[],
    )
    rendered = render_review_markdown(verdict, [], [])
    assert "## 🛡️ Code Review: `APPROVE`" in rendered
    assert "Clean README documentation update." in rendered


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_core_backend_deep_audit_rendering() -> None:
    risk = RiskItem(
        category="concurrency",
        file="src/logic/state.py",
        line_range="L45-L50",
        description="Unhandled race condition during shared state mutation.",
        remediation="Wrap update in asyncio.Lock() context manager.",
    )
    verdict = AuditVerdict(
        verdict="REQUEST_CHANGES",
        confidence=4.5,
        pr_type="core_backend",
        summary="Race condition identified in core backend state updater.",
        risks=[risk],
    )
    rendered = render_review_markdown(verdict, [risk], [])
    assert "`REQUEST_CHANGES`" in rendered
    assert "[CONCURRENCY]" in rendered
    assert "`src/logic/state.py:L45-L50`" in rendered
    assert "asyncio.Lock()" in rendered
