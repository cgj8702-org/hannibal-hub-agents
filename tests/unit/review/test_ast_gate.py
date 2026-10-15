"""Unit tests for AST Syntactic Gate for LLM review code suggestions."""

from __future__ import annotations

import pytest

from webhook_agent.review.ast_gate import (
    filter_invalid_ast_suggestions,
    is_python_file,
    strip_code_fences,
    validate_code_suggestion,
    validate_python_syntax,
)
from webhook_agent.review.comment_poster import (
    build_github_review_comments,
    format_suggestion_body,
)
from webhook_agent.review.schemas import IssueItem

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_strip_code_fences() -> None:
    assert strip_code_fences("x = 1") == "x = 1"
    assert strip_code_fences("```python\nx = 1\n```") == "x = 1"
    assert strip_code_fences("```\nfoo()\n```") == "foo()"
    assert strip_code_fences("```typescript\nconst x = 1;\n```") == "const x = 1;"


def test_is_python_file() -> None:
    assert is_python_file("src/main.py") is True
    assert is_python_file("types.pyi") is True
    assert is_python_file("README.md") is False
    assert is_python_file("deploy.yaml") is False
    assert is_python_file(None) is False
    assert is_python_file("") is False


def test_validate_python_syntax_valid() -> None:
    valid, err = validate_python_syntax("x = 10\ny = x + 5")
    assert valid is True
    assert err is None

    # Empty snippet
    valid, err = validate_python_syntax("   ")
    assert valid is True
    assert err is None

    # Dedented function snippet
    valid, err = validate_python_syntax("    if foo:\n        bar()")
    assert valid is True
    assert err is None


def test_validate_python_syntax_contextual_keywords() -> None:
    # return statement outside explicit function should be wrapped and pass
    valid, err = validate_python_syntax("return x + 1")
    assert valid is True
    assert err is None

    # yield statement
    valid, err = validate_python_syntax("yield chunk")
    assert valid is True
    assert err is None

    # break / continue in loop
    valid, err = validate_python_syntax("break")
    assert valid is True
    assert err is None

    valid, err = validate_python_syntax("continue")
    assert valid is True
    assert err is None


def test_validate_python_syntax_invalid() -> None:
    # Syntax error: unclosed parenthesis
    valid, err = validate_python_syntax("def foo(x, y: return x")
    assert valid is False
    assert err is not None
    assert "SyntaxError" in err

    # Broken indentation
    valid, err = validate_python_syntax("if True:\nx = 1")
    assert valid is False
    assert err is not None


def test_validate_code_suggestion_non_python() -> None:
    # Non-Python files bypass AST gate
    valid, err = validate_code_suggestion("const x = { broken::: 1 }", file_path="app.ts")
    assert valid is True
    assert err is None


def test_filter_invalid_ast_suggestions_clears_broken_python() -> None:
    issues = [
        IssueItem(
            path="src/logic.py",
            line=10,
            description="Fix calculation",
            suggested_fix="def broken(x: return",
        ),
        IssueItem(
            path="src/valid.py",
            line=20,
            description="Clean code",
            suggested_fix="x = 42\nreturn x",
        ),
        IssueItem(
            path="config.yaml",
            line=5,
            description="Fix yaml",
            suggested_fix="invalid: [syntax: unclosed",
        ),
    ]

    filtered = filter_invalid_ast_suggestions(issues)
    assert len(filtered) == 3
    # Invalid Python fix was stripped
    assert filtered[0].suggested_fix == ""
    # Valid Python fix remained
    assert filtered[1].suggested_fix == "x = 42\nreturn x"
    # Non-Python file fix remained
    assert filtered[2].suggested_fix == "invalid: [syntax: unclosed"


def test_filter_invalid_ast_suggestions_dict_support() -> None:
    dict_issues = [
        {
            "path": "test.py",
            "line": 15,
            "description": "Typo",
            "suggested_fix": "def (bad: syntax",
        }
    ]
    filtered = filter_invalid_ast_suggestions(dict_issues)
    assert filtered[0]["suggested_fix"] == ""


def test_format_suggestion_body_skips_invalid_ast() -> None:
    body = format_suggestion_body(
        description="Fix the function header",
        suggested_fix="def invalid(x, y: return",
        file_path="src/engine.py",
    )
    # The suggested_fix is suppressed because it's invalid Python AST
    assert "```suggestion" not in body
    assert body == "Fix the function header"


def test_format_suggestion_body_preserves_valid_ast() -> None:
    body = format_suggestion_body(
        description="Fix the function header",
        suggested_fix="return x + y",
        file_path="src/engine.py",
    )
    assert "```suggestion" in body
    assert "return x + y" in body


def test_build_github_review_comments_strips_invalid_ast() -> None:
    diff_text = (
        "--- a/src/engine.py\n+++ b/src/engine.py\n@@ -10,2 +10,2 @@\n- old_code\n+ new_code\n"
    )
    issues = [
        IssueItem(
            path="src/engine.py",
            line=10,
            description="Syntax bug",
            suggested_fix="def broken(:",
        )
    ]
    comments, _anchored = build_github_review_comments(issues, diff_text)
    assert len(comments) == 1
    # Comment exists but suggestion block was filtered out
    assert "```suggestion" not in comments[0]["body"]
    assert "Syntax bug" in comments[0]["body"]
