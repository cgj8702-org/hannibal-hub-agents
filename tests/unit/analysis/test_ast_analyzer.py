"""Unit tests for AST-aware diff chunk analyzer and risk scoring."""

from __future__ import annotations

import pytest

from webhook_agent.analysis.ast_analyzer import (
    analyze_diff_hunks,
    analyze_python_code,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_analyze_empty_code() -> None:
    res = analyze_python_code("")
    assert res.risk_score == 0.0
    assert res.nodes_analyzed == 0
    assert res.impact_counts["BREAKING_SIGNATURE"] == 0


def test_analyze_function_def() -> None:
    code = "def my_func(a: int, b: str) -> bool:\n    return True\n"
    res = analyze_python_code(code)
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.6
    assert "BREAKING_SIGNATURE" in res.categories_found


def test_analyze_class_def() -> None:
    code = "class MyBaseClass:\n    pass\n\nclass MySubClass(MyBaseClass):\n    def method(self):\n        pass\n"
    res = analyze_python_code(code)
    assert res.impact_counts["CLASS_INHERITANCE"] == 2
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.5


def test_analyze_internal_logic() -> None:
    code = "x = 10\nif x > 5:\n    x += 1\n"
    res = analyze_python_code(code)
    assert res.impact_counts["INTERNAL_LOGIC"] > 0
    assert res.risk_score < 0.8


def test_analyze_diff_hunks() -> None:
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "index 1234567..89abcdef 100644\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1,3 +1,5 @@\n"
        "+def new_public_api(param: str) -> None:\n"
        "+    x = 1\n"
        "+    return x\n"
    )
    res = analyze_diff_hunks(diff)
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.6
    assert res.nodes_analyzed > 0
