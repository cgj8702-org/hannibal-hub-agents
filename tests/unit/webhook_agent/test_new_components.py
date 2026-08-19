"""Unit tests for multi-agent ADK audit pipeline components."""

from __future__ import annotations

import pytest
from webhook_agent.audit_schema import AuditVerdict, RiskItem
from webhook_agent.comment_poster import (
    prepare_review_payload,
    sanitize_and_anchor_risks,
)
from webhook_agent.diff_tools import get_pr_diff_file_map, verify_line_reference
from webhook_agent.sanitizer_plugin import sanitize_markdown_text

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_audit_schema_clean_pr() -> None:
    verdict = AuditVerdict(
        verdict="APPROVE",
        confidence=5.0,
        pr_type="dev_docs",
        summary="Clean documentation update.",
        risks=[],
    )
    assert verdict.verdict == "APPROVE"
    assert verdict.risks == []


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.guardrails
def test_sanitizer_plugin_prompt_leakage_and_secrets() -> None:
    raw_text = "> [!IMPORTANT] Finding zero risks...\nAPI Key: AIzaSy123456789012345678901234567890123"
    sanitized = sanitize_markdown_text(raw_text)
    assert "[!IMPORTANT] Finding zero risks" not in sanitized
    assert "AIzaSy123456789012345678901234567890123" not in sanitized
    assert "[REDACTED_SECRET]" in sanitized


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_diff_tools_line_verification() -> None:
    sample_diff = (
        "diff --git a/src/main.py b/src/main.py\n"
        "index 100..200 100644\n"
        "--- a/src/main.py\n"
        "+++ b/src/main.py\n"
        "@@ -10,5 +10,2 @@\n"
        "+new_line_1\n"
        "+new_line_2\n"
    )

    summary = get_pr_diff_file_map(sample_diff)
    assert "src/main.py" in summary["modified_files"]

    assert verify_line_reference(sample_diff, "src/main.py", 10) is True
    assert verify_line_reference(sample_diff, "src/main.py", 99) is False


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_comment_poster_out_of_diff_pruning() -> None:
    sample_diff = (
        "diff --git a/src/main.py b/src/main.py\n"
        "index 100..200 100644\n"
        "--- a/src/main.py\n"
        "+++ b/src/main.py\n"
        "@@ -10,5 +10,2 @@\n"
        "+new_line_1\n"
        "+new_line_2\n"
    )
    risks = [
        RiskItem(
            category="concurrency",
            file="src/main.py",
            line_range="L10",
            description="Valid diff line risk",
            remediation="Add lock",
        ),
        RiskItem(
            category="memory",
            file="src/main.py",
            line_range="L999",
            description="Out of diff hunk risk",
            remediation="Refactor function",
        ),
    ]

    anchored, unanchored = sanitize_and_anchor_risks(risks, sample_diff)
    assert len(anchored) == 1
    assert len(unanchored) == 1

    verdict = AuditVerdict(
        verdict="REQUEST_CHANGES",
        confidence=4.5,
        pr_type="core_backend",
        summary="Backend audit findings.",
        risks=risks,
    )
    payload = prepare_review_payload(verdict, sample_diff)
    assert payload["event"] == "REQUEST_CHANGES"
    assert "Hannibal Hub Audit Report" in payload["body"]
