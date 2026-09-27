"""Unit tests for AST-aware diff chunk analyzer and risk scoring."""

from __future__ import annotations

import pytest

from webhook_agent.logic.ast_analyzer import (
    ASTAnalysisResult,
    analyze_diff_hunks,
    analyze_python_code,
)


def test_analyze_empty_code() -> None:
    res = analyze_python_code("")
    assert res.risk_score == 0.0
    assert res.nodes_analyzed == 0
    assert res.impact_counts["BREAKING_SIGNATURE"] == 0


def test_analyze_function_def() -> None:
    code = """
def my_func(a: int, b: str) -> bool:
    return True
"""
    res = analyze_python_code(code)
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.6
    assert "BREAKING_SIGNATURE" in res.categories_found


def test_analyze_class_def_with_bases() -> None:
    code = """
class MyBaseClass:
    pass

class MySubClass(MyBaseClass):
    def method(self):
        pass
"""
    res = analyze_python_code(code)
    assert res.impact_counts["CLASS_INHERITANCE"] == 1
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.6


def test_analyze_internal_logic() -> None:
    code = """
x = 10
if x > 5:
    x += 1
"""
    res = analyze_python_code(code)
    assert res.impact_counts["INTERNAL_LOGIC"] > 0
    assert res.risk_score < 0.8


def test_analyze_diff_hunks() -> None:
    diff = """
diff --git a/src/foo.py b/src/foo.py
index 1234567..89abcdef 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,5 @@
+def new_public_api(param: str) -> None:
+    """Docstring comment"""
     x = 1
     return x
"""
    res = analyze_diff_hunks(diff)
    assert res.impact_counts["BREAKING_SIGNATURE"] == 1
    assert res.risk_score >= 0.6
    assert res.nodes_analyzed > 0
