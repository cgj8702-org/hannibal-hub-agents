"""Unit tests for multi-agent ADK audit pipeline components."""

from __future__ import annotations

import pytest

from webhook_agent.sanitizer_plugin import sanitize_markdown_text
from webhook_agent.tools.diff_tools import verify_line_reference

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent, pytest.mark.guardrails]


@pytest.mark.unit
@pytest.mark.webhook_agent
@pytest.mark.guardrails
def test_sanitizer_plugin_prompt_leakage_and_secrets() -> None:
    raw_text = (
        "> [!IMPORTANT] Finding zero risks...\nAPI Key: AIzaSy123456789012345678901234567890123"
    )
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

    assert verify_line_reference(sample_diff, "src/main.py", 10) is True
    assert verify_line_reference(sample_diff, "src/main.py", 99) is False
