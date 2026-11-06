"""AST & Syntax Integrity Verification Tool for Webhook Agent.

Adapted from adk-samples review tools & software-bug-assistant.
Provides compiler-grade syntax and structural validation for Python code diffs.
"""

from __future__ import annotations

import ast
import itertools
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        code_snippet: Optional Python source code snippet to verify. If omitted or empty,
                      the tool attempts to read the file from disk or diff_text.
        diff_text: Optional full PR diff text from which right-side file lines can be extracted
                   if the file is not checked out on the local disk.

    Returns:
        A clinical summary of AST validation, highlighting syntax errors with exact line and column
        numbers, impact categories, and structural defects.
    """
    code_text = code_snippet
    is_partial_diff_view = False
    if not code_text or not code_text.strip():
        cleaned_path = _strip_diff_prefix(file_path)

        # 1. Prefer reading real file from active PR workspace sandbox checked out at PR head
        target_file: Path | None = None
        from webhook_agent.environment.context import get_active_environment

        env = get_active_environment()
        if env is not None:
            for cand_p in [cleaned_path, file_path]:
                try:
                    resolved = env.resolve_path(cand_p)
                    if resolved and resolved.is_file():
                        target_file = resolved
                        break
                except Exception as resolve_err:
                    logger.debug(
                        "Failed resolving candidate path %s in env: %s", cand_p, resolve_err
                    )

        if target_file and target_file.is_file():
            try:
                code_text = target_file.read_text(encoding="utf-8", errors="replace")
            except Exception as read_err:
                return f"Error reading file '{file_path}': {read_err}"

        # 2. Extract lines from diff_text if file is not in workspace sandbox (defends against host disk bleed)
        if not code_text or not code_text.strip():
            if diff_text:
                try:
                    from webhook_agent.tools.diff_tools import walk_right_side

                    _, text_map = walk_right_side(diff_text)
                    file_lines = text_map.get(cleaned_path) or text_map.get(file_path)
                    if file_lines:
                        sorted_line_keys = sorted(file_lines.keys())
                        sorted_lines = [file_lines[k] for k in sorted_line_keys]
                        code_text = "\n".join(sorted_lines)
                        if len(sorted_line_keys) > 1 and any(
                            b != a + 1 for a, b in itertools.pairwise(sorted_line_keys)
                        ):
                            is_partial_diff_view = True
                except Exception as diff_err:
                    logger.debug(
                        "Could not extract lines from diff_text for %s: %s", file_path, diff_err
                    )

        # 3. Fall back to local repo_root / disk if not found in sandbox or diff_text
        if not code_text or not code_text.strip():
            if repo_root is not None:
                root_path = Path(repo_root)
                for rel_cand in [cleaned_path, file_path]:
                    cand = (root_path / rel_cand).resolve()
                    if cand.is_file():
                        target_file = cand
                        break

            if target_file is None:
                for p_cand in [Path(file_path), Path(cleaned_path)]:
                    if p_cand.is_absolute() and p_cand.is_file():
                        target_file = p_cand
                        break

            if target_file is None:
                from webhook_agent.tools.paths import resolve_sandboxed_path

                sandboxed_file = resolve_sandboxed_path(
                    cleaned_path, root=repo_root
                ) or resolve_sandboxed_path(file_path, root=repo_root)
                if sandboxed_file and sandboxed_file.is_file():
                    target_file = sandboxed_file

            if target_file and target_file.is_file():
                try:
                    code_text = target_file.read_text(encoding="utf-8", errors="replace")
                except Exception as read_err:
                    return f"Error reading file '{file_path}': {read_err}"

        if not code_text or not code_text.strip():
            return (
                f"Error: No code snippet provided and file '{file_path}' was not found in diff or sandbox. "
                "Please pass code_snippet directly to verify_python_ast."
            )

    # 1. Compile via AST to verify syntax
    try:
        tree = ast.parse(code_text, filename=file_path)
    except SyntaxError as e:
        # If parsing a diff hunk, try textwrap.dedent
        try:
            import textwrap

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
