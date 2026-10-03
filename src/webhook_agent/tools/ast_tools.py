"""AST & Syntax Integrity Verification Tool for Webhook Agent.

Adapted from adk-samples review tools & software-bug-assistant.
Provides compiler-grade syntax and structural validation for Python code diffs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from google.adk.tools import FunctionTool

from webhook_agent.logic.ast_analyzer import analyze_python_code


class StructuralDefectVisitor(ast.NodeVisitor):
    """AST visitor that checks for common code smells and syntax defects."""

    def __init__(self) -> None:
        self.defects: list[str] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.defects.append(
                f"Line {node.lineno}: Bare 'except:' clause catches BaseException (should specify Exception)."
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def _check_mutable_defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
        for d in defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                type_name = type(d).__name__.lower()
                self.defects.append(
                    f"Line {node.lineno}: Mutable default argument ({type_name}) in function '{node.name}'."
                )

    def _check_body_unreachable(self, body: list[Any]) -> None:
        for idx, stmt in enumerate(body[:-1]):
            if isinstance(stmt, (ast.Return, ast.Raise)):
                unreachable = body[idx + 1]
                lineno = getattr(unreachable, "lineno", stmt.lineno)
                self.defects.append(
                    f"Line {lineno}: Unreachable statement after {type(stmt).__name__.lower()} on line {stmt.lineno}."
                )
                break

    def generic_visit(self, node: ast.AST) -> None:
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(node, attr, None)
            if isinstance(body, list):
                self._check_body_unreachable(body)
        super().generic_visit(node)


def _strip_diff_prefix(path: str) -> str:
    """Drop git's a/ or b/ diff prefix from a path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def verify_python_ast(
    file_path: str,
    code_snippet: str | None = None,
) -> str:
    """Verify syntax, AST structure, and potential runtime defects in Python code.

    Parses code with the Python AST compiler to verify syntax correctness, check for
    common structural defects (e.g. bare excepts, mutable default arguments, unreachable statements),
    and calculate AST impact scores.

    Args:
        file_path: Path to the target Python file (e.g. 'src/webhook_agent/logic/model_chain.py').
        code_snippet: Optional Python source code snippet to verify. If omitted or empty,
                      the tool attempts to read the file from disk if accessible.

    Returns:
        A clinical summary of AST validation, highlighting syntax errors with exact line and column
        numbers, impact categories, and structural defects.
    """
    code_text = code_snippet
    if not code_text or not code_text.strip():
        cleaned_path = _strip_diff_prefix(file_path)
        candidates = [Path(file_path), Path(cleaned_path)]
        target_file = next((p for p in candidates if p.is_file()), None)
        if target_file:
            try:
                code_text = target_file.read_text(encoding="utf-8", errors="replace")
            except Exception as read_err:
                return f"Error reading file '{file_path}': {read_err}"
        else:
            return (
                f"Error: No code snippet provided and file '{file_path}' was not found on disk. "
                "Please pass code_snippet directly to verify_python_ast."
            )

    # 1. Compile via AST to verify syntax
    try:
        tree = ast.parse(code_text, filename=file_path)
    except SyntaxError as e:
        error_line = e.lineno or 0
        error_col = e.offset or 0
        snippet = f"\n  >>> {e.text.strip()}" if e.text else ""
        return (
            f"❌ SyntaxError in '{file_path}' at line {error_line}, col {error_col}: {e.msg}{snippet}\n"
            f"Action: Review and repair the syntax error at line {error_line}."
        )

    # 2. Structural defects inspection
    visitor = StructuralDefectVisitor()
    visitor.visit(tree)

    # 3. Impact scoring via ast_analyzer
    analysis_res = analyze_python_code(code_text)
    categories_str = (
        ", ".join(analysis_res.categories_found) if analysis_res.categories_found else "None"
    )

    lines = [
        f"✅ AST Verification for '{file_path}':",
        "- Status: SYNTAX VALID",
        f"- Risk Score: {analysis_res.risk_score:.2f}",
        f"- Nodes Analyzed: {analysis_res.nodes_analyzed}",
        f"- Impact Categories: {categories_str}",
    ]

    if visitor.defects:
        lines.append("- Structural Defects / Warnings:")
        for defect in visitor.defects:
            lines.append(f"  * ⚠️ {defect}")
    else:
        lines.append("- Structural Defects: None detected.")

    return "\n".join(lines)


verify_python_ast_tool = FunctionTool(verify_python_ast)
