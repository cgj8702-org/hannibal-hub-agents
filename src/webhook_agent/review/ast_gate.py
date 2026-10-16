"""AST Syntactic Gate for LLM Review Code Suggestions.

Validates that code suggestions proposed in review findings represent valid Python syntax
before they are formatted into GitHub review comments or suggestions, eliminating broken
indentation and syntax hallucination.
"""

from __future__ import annotations

import ast
import logging
import re
import textwrap
from typing import Any

logger = logging.getLogger("webhook_agent.review.ast_gate")

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?(.*?)\n?```$", re.DOTALL)


def strip_code_fences(code: str) -> str:
    """Extract code body from markdown code blocks or backtick fences."""
    trimmed = code.strip()
    match = _CODE_FENCE_RE.match(trimmed)
    if match:
        return match.group(1).strip("\r\n")
    return trimmed.strip("\r\n")


def validate_python_syntax(code_snippet: str) -> tuple[bool, str | None]:
    """Check if code_snippet represents parseable Python code.

    Handles indented function/loop bodies (dedents automatically) and returns/yields
    by wrapping inside a dummy function scope when needed.
    """
    clean_code = strip_code_fences(code_snippet).strip()
    if not clean_code:
        return True, None

    dedented = textwrap.dedent(clean_code)

    # 1. Direct AST parse attempt
    try:
        ast.parse(dedented)
        return True, None
    except (SyntaxError, IndentationError) as first_err:
        first_msg = str(first_err).lower()
        # If failure is due to statements valid only in functions/loops (return, yield, break, continue)
        if any(
            phrase in first_msg
            for phrase in (
                "'return' outside function",
                "'yield' outside function",
                "'break' outside loop",
                "'continue' not properly in loop",
                "non-default argument follows default argument",
            )
        ):
            try:
                wrapped = f"def _dummy_context():\n    while True:\n{textwrap.indent(dedented, '        ')}"
                ast.parse(wrapped)
                return True, None
            except Exception as wrapped_err:
                return False, f"{type(wrapped_err).__name__}: {wrapped_err}"

        # If failure is due to single-line block headers (if, elif, else, try, except, finally, with, def, class)
        if any(
            phrase in first_msg
            for phrase in (
                "expected an indented block",
                "unexpected eof while parsing",
            )
        ) or dedented.strip().endswith(":"):
            for template in (
                f"{dedented}\n    pass",
                f"if True:\n    pass\n{dedented}\n    pass",
                f"try:\n    pass\n{dedented}\n    pass",
            ):
                try:
                    ast.parse(template)
                    return True, None
                except Exception:  # noqa: S110 - trial parsing of template wrappers, error expected
                    pass

        # If it's a multiline or single line expression/assignment, try evaluating as single statement
        try:
            ast.parse(f"(\n{dedented}\n)")
            return True, None
        except Exception:  # noqa: S110 - trial parsing of expression wrap, error expected
            pass

        return False, f"{type(first_err).__name__}: {first_err}"


def is_python_file(file_path: str | None) -> bool:
    """Return True if the file target is a Python source file."""
    if not file_path:
        return False
    norm = file_path.lower().strip()
    return norm.endswith((".py", ".pyi"))


def validate_code_suggestion(suggested_fix: str, file_path: str = "") -> tuple[bool, str | None]:
    """Validate a code fix suggestion against AST invariants if target file is Python.

    Non-Python files pass validation by default.
    """
    if not suggested_fix or not suggested_fix.strip():
        return True, None

    if file_path and not is_python_file(file_path):
        return True, None

    return validate_python_syntax(suggested_fix)


def filter_invalid_ast_suggestions(issues: list[Any]) -> list[Any]:
    """Inspect review issues and sanitize suggested_fix if it contains invalid Python AST."""
    if not issues:
        return issues

    for issue in issues:
        path = getattr(issue, "path", None) or (
            issue.get("path") if isinstance(issue, dict) else None
        )
        fix = getattr(issue, "suggested_fix", None) or (
            issue.get("suggested_fix") if isinstance(issue, dict) else None
        )
        if not path or not fix or not is_python_file(str(path)):
            continue

        valid, error_msg = validate_code_suggestion(str(fix), file_path=str(path))
        if not valid:
            line_no = getattr(issue, "line", None) or (
                issue.get("line") if isinstance(issue, dict) else "?"
            )
            logger.warning(
                "🛡️ AST Gate: Stripping syntactically invalid code suggestion at %s:%s: %s",
                path,
                line_no,
                error_msg,
            )
            if hasattr(issue, "suggested_fix"):
                issue.suggested_fix = ""
            elif isinstance(issue, dict):
                issue["suggested_fix"] = ""

    return issues
