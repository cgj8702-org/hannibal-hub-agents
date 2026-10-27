"""Unit tests for clean_field_string universal sanitizer."""

from __future__ import annotations

import pytest

from webhook_agent.review.schemas import clean_field_string

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.mark.parametrize(
    ("input_str", "expected"),
    [
        ("`test_evidence_quote`", "`test_evidence_quote`"),
        ("• `path/to/file.py`: description", "`path/to/file.py`: description"),
        ("- `src/foo.py`: line 10", "`src/foo.py`: line 10"),
        ("`path/to/file.py`: description", "`path/to/file.py`: description"),
        ("`path/to/file.py:10` some description", "`path/to/file.py:10` some description"),
        ("`foo`", "`foo`"),
        ("* `foo`", "`foo`"),
        ("**Risk**: `foo`", "`foo`"),
        ("**Risk:** `foo`", "`foo`"),
        ("Risk: `foo`", "Risk: `foo`"),
        ("• **Evidence:** `line_of_code`", "`line_of_code`"),
        ("**Dimension**: Logic & Boundaries", "Logic & Boundaries"),
        ("🔴 [UNRESOLVED] **Risk:** High memory usage", "High memory usage"),
        ("✅ [RESOLVED] Fixed leak in `handler.py`", "Fixed leak in `handler.py`"),
        ("• Bullet text without key", "Bullet text without key"),
        ("Regular text with no markup", "Regular text with no markup"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_field_string_cases(input_str: str, expected: str) -> None:
    assert clean_field_string(input_str) == expected


def test_clean_field_string_non_string() -> None:
    assert clean_field_string(None) == ""
    assert clean_field_string(123) == 123
    assert clean_field_string(["list"]) == ["list"]
