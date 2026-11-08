"""AST & Syntax Integrity Verification Scanner for Webhook Agent.

Provides compiler-grade syntax and structural validation for Python code diffs.
Offline static analyzer residing strictly within the analysis layer.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from pathlib import Path
from typing import Any

logger = logging.getLogger("webhook_agent.analysis.ast_scanner")


class StructuralDefectVisitor(ast.NodeVisitor):
    """AST visitor that checks for common code smells and syntax defects."""

    def __init__(self, skip_unreachable: bool = False) -> None:
        self.defects: list[str] = []
        self.skip_unreachable = skip_unreachable

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
        if self.skip_unreachable:
            return
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


def _extract_lines_from_diff(diff_text: str, target_path: str) -> tuple[str | None, bool]:
    """Extract right-side added/modified lines for a file from unified diff text."""
    cleaned_target = _strip_diff_prefix(target_path)
    lines: list[str] = []
    in_target_file = False
    is_partial = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                b_path = _strip_diff_prefix(parts[3])
                in_target_file = b_path in (cleaned_target, target_path)
            else:
                in_target_file = False
            continue

        if in_target_file:
            if line.startswith("@@"):
                is_partial = True
            elif line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
            elif not line.startswith("-") and not line.startswith("---"):
                lines.append(line[1:] if line.startswith(" ") else line)

    if lines:
        return "\n".join(lines), is_partial
    return None, False


def verify_python_ast(
    file_path: str,
    code_snippet: str | None = None,
    diff_text: str | None = None,
    repo_root: Path | str | None = None,
) -> str:
    """Verify syntax, AST structure, and potential runtime defects in Python code.

    Parses code with the Python AST compiler to verify syntax correctness, check for
    common structural defects (e.g. bare excepts, mutable default arguments, unreachable statements),
    and calculate AST impact scores.

    Args:
        file_path: Path to the target Python file (e.g. 'src/webhook_agent/models/model_chain.py').
        code_snippet: Optional Python source code snippet to verify.
        diff_text: Optional full PR diff text to extract lines if file not on disk.
        repo_root: Optional repository root path on disk.

    Returns:
        A clinical summary of AST validation.
    """
    code_text = code_snippet
    is_partial_diff_view = False
    cleaned_path = _strip_diff_prefix(file_path)

    if not code_text or not code_text.strip():
        # 1. Try disk resolution from repo_root or current working directory
        root_path = Path(repo_root) if repo_root is not None else Path.cwd()
        for cand_rel in [cleaned_path, file_path]:
            cand_file = root_path / cand_rel
            if cand_file.is_file():
                try:
                    code_text = cand_file.read_text(encoding="utf-8", errors="replace")
                    break
                except Exception as read_err:
                    return f"Error reading file '{file_path}': {read_err}"

        # 2. Extract from diff_text if not found on disk
        if not code_text or not code_text.strip():
            if diff_text:
                extracted, partial = _extract_lines_from_diff(diff_text, cleaned_path)
                if extracted:
                    code_text = extracted
                    is_partial_diff_view = partial

        if not code_text or not code_text.strip():
            return (
                f"Error: No code snippet provided and file '{file_path}' was not found in diff or sandbox. "
                "Please pass code_snippet directly to verify_python_ast."
            )

    # 1. Compile via AST to verify syntax
    try:
        tree = ast.parse(code_text, filename=file_path)
    except SyntaxError as e:
        try:
            tree = ast.parse(textwrap.dedent(code_text), filename=file_path)
        except SyntaxError:
            if is_partial_diff_view:
                return f"Could not parse partial diff fragment; this is expected for hunks ({file_path})."
            error_line = e.lineno or 0
            error_col = e.offset or 0
            snippet = f"\n  >>> {e.text.strip()}" if e.text else ""
            return (
                f"❌ SyntaxError in '{file_path}' at line {error_line}, col {error_col}: {e.msg}{snippet}\n"
                f"Action: Review and repair the syntax error at line {error_line}."
            )

    # 2. Structural defects inspection
    visitor = StructuralDefectVisitor(skip_unreachable=is_partial_diff_view)
    visitor.visit(tree)

    # 3. Node count inspection
    nodes_analyzed = len(list(ast.walk(tree)))
    status_str = "PARSE OK (partial diff view)" if is_partial_diff_view else "SYNTAX VALID"
    lines = [
        f"✅ AST Verification for '{file_path}':",
        f"- Status: {status_str}",
        f"- Nodes Analyzed: {nodes_analyzed}",
    ]

    if visitor.defects:
        lines.append("- Structural Defects / Warnings:")
        for defect in visitor.defects:
            lines.append(f"  * ⚠️ {defect}")
    else:
        lines.append("- Structural Defects: None detected.")

    return "\n".join(lines)


__all__ = [
    "StructuralDefectVisitor",
    "verify_python_ast",
]
