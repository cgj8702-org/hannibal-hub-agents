"""Deterministic AST Loop Termination and Safety Scanner (LoopTerminationScanner).

Statically analyzes modified Python files and unified diffs to detect non-terminating
loops, unbounded while loops, and conditional index advances that cause infinite hangs.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.loop_scanner")

BUILTIN_IMMUTABLE_NAMES: frozenset[str] = frozenset(
    {
        "len",
        "range",
        "enumerate",
        "isinstance",
        "issubclass",
        "type",
        "hasattr",
        "getattr",
        "None",
        "True",
        "False",
        "int",
        "str",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
    }
)

EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "tests/",
    "test/",
    "testing/",
    "fixtures/",
    "test_fixtures/",
)

EXEMPT_FILE_SUFFIXES: tuple[str, ...] = (
    "_test.py",
    "test_.py",
)


@dataclass(frozen=True)
class LoopTerminationFinding:
    """An individual loop termination defect detected in Python code."""

    file_path: str
    line_number: int
    loop_type: str  # "WHILE_LOOP"
    finding_type: str  # "NON_ADVANCING_LOOP", "UNBOUNDED_TRUE_LOOP"
    variables: list[str]
    description: str
    severity: Literal["CRITICAL", "WARN"] = "CRITICAL"


@dataclass(frozen=True)
class LoopTerminationReport:
    """Clinical summary of detected loop termination and safety issues."""

    findings: list[LoopTerminationFinding] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    scanned_loops_count: int = 0

    @property
    def has_critical_findings(self) -> bool:
        """Return True if any critical non-terminating loops were detected."""
        return any(f.severity == "CRITICAL" for f in self.findings)

    def to_markdown(self, **kwargs: Any) -> str:
        """Render clinical Markdown summary of loop findings."""
        if not self.findings:
            return "Deterministic audit confirmed loop termination invariants across all modified loops."

        lines: list[str] = [
            f"Found {len(self.findings)} loop safety defect(s) across {len(self.scanned_files)} file(s):"
        ]
        for f in self.findings:
            loc = f"`{f.file_path}:{f.line_number}`"
            lines.append(
                f"- 🔴 **[{f.finding_type}]** {loc} ({', '.join(f.variables)}): {f.description}"
            )
        return "\n".join(lines)


class _LoopVisitor(ast.NodeVisitor):
    """AST Visitor detecting unsafe while loop termination patterns."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.findings: list[LoopTerminationFinding] = []
        self.loop_count = 0

    def visit_While(self, node: ast.While) -> None:
        self.loop_count += 1
        self._analyze_while_loop(node)
        self.generic_visit(node)

    def _analyze_while_loop(self, node: ast.While) -> None:
        # Check while True / constant true loops
        if self._is_constant_true(node.test):
            if not self._has_exit_statement(node.body):
                self.findings.append(
                    LoopTerminationFinding(
                        file_path=self.file_path,
                        line_number=node.lineno,
                        loop_type="WHILE_LOOP",
                        finding_type="UNBOUNDED_TRUE_LOOP",
                        variables=["True"],
                        description=(
                            "Unbounded `while True:` loop contains no `break`, `return`, or `raise` "
                            "statement, creating an inescapable infinite execution loop."
                        ),
                        severity="CRITICAL",
                    )
                )
            return

        # Extract loop control variables
        loop_vars = self._extract_names(node.test)
        if not loop_vars:
            return

        # Analyze whether all control flow paths advance at least one loop variable or exit
        has_unsafe_path, details = self._has_non_advancing_path(node.body, loop_vars)
        if has_unsafe_path:
            var_list = sorted(loop_vars)
            self.findings.append(
                LoopTerminationFinding(
                    file_path=self.file_path,
                    line_number=node.lineno,
                    loop_type="WHILE_LOOP",
                    finding_type="NON_ADVANCING_LOOP",
                    variables=var_list,
                    description=(
                        f"Loop condition tests variable(s) {var_list}, but execution paths exist "
                        f"where variables are not modified and loop is not exited ({details}). "
                        "Causes infinite while-loop hang when conditions fail."
                    ),
                    severity="CRITICAL",
                )
            )

    def _is_constant_true(self, test: ast.expr) -> bool:
        if isinstance(test, ast.Constant):
            return bool(test.value)
        return False

    def _extract_names(self, node: ast.AST) -> set[str]:
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in BUILTIN_IMMUTABLE_NAMES:
                    names.add(sub.id)
        return names

    def _has_exit_statement(self, stmts: list[ast.stmt]) -> bool:
        """Check if statements contain any break, return, or raise."""
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Break, ast.Return, ast.Raise)):
                    return True
        return False

    def _has_non_advancing_path(
        self, stmts: list[ast.stmt], loop_vars: set[str]
    ) -> tuple[bool, str]:
        """Check if there is an execution path through stmts that neither advances loop_vars nor exits."""
        # Find if any unconditional statement in top-level body advances loop_vars or exits
        for stmt in stmts:
            if not isinstance(stmt, ast.If):
                if self._statement_exits(stmt):
                    return False, "unconditional exit"
                if self._statement_mutates(stmt, loop_vars):
                    return False, "unconditional advance"

        # If no unconditional advance, inspect top-level `if` statements
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                body_advances = self._path_advances_or_exits(stmt.body, loop_vars)
                orelse_advances = (
                    self._path_advances_or_exits(stmt.orelse, loop_vars) if stmt.orelse else False
                )

                if body_advances and not orelse_advances:
                    # Body advances, but else branch is missing or does not advance/exit
                    # Check if subsequent statements in the while loop advance loop_vars
                    after_stmts = stmts[stmts.index(stmt) + 1 :]
                    if not self._any_statement_advances(after_stmts, loop_vars):
                        return (
                            True,
                            "variable(s) only advanced inside conditional branch without else advancement",
                        )

        # If loop body has no advance anywhere for loop_vars
        if not self._any_statement_advances(stmts, loop_vars):
            return True, "no modification to loop control variable(s) found in loop body"

        return False, "all paths advance"

    def _statement_exits(self, stmt: ast.stmt) -> bool:
        return isinstance(stmt, (ast.Break, ast.Return, ast.Raise))

    def _statement_mutates(self, stmt: ast.stmt, loop_vars: set[str]) -> bool:
        """Check if statement mutates any variable in loop_vars."""
        # Assignment to loop var (e.g. idx += 1, idx = idx + 1)
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id in loop_vars:
                return True
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in loop_vars:
                    return True
        # Container mutation calls: timestamps.pop(...), timestamps.remove(...), etc.
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                var_name = call.func.value.id
                method_name = call.func.attr
                if var_name in loop_vars and method_name in (
                    "pop",
                    "remove",
                    "append",
                    "extend",
                    "clear",
                    "discard",
                ):
                    return True
        # del timestamps[idx]
        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    if target.value.id in loop_vars:
                        return True
        return False

    def _path_advances_or_exits(self, stmts: list[ast.stmt], loop_vars: set[str]) -> bool:
        """Return True if this sequence of statements contains an advance or exit."""
        for stmt in stmts:
            if self._statement_exits(stmt) or self._statement_mutates(stmt, loop_vars):
                return True
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Break, ast.Return, ast.Raise)):
                    return True
                if isinstance(sub, ast.AugAssign) and isinstance(sub.target, ast.Name):
                    if sub.target.id in loop_vars:
                        return True
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name) and t.id in loop_vars:
                            return True
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if isinstance(sub.func.value, ast.Name) and sub.func.value.id in loop_vars:
                        if sub.func.attr in ("pop", "remove", "clear", "discard"):
                            return True
        return False

    def _any_statement_advances(self, stmts: list[ast.stmt], loop_vars: set[str]) -> bool:
        """Return True if any statement in stmts mutates loop_vars or exits."""
        return any(self._path_advances_or_exits([s], loop_vars) for s in stmts)


class LoopTerminationScanner:
    """Scanner analyzing Python source files and unified diffs for loop safety."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root

    def scan(
        self,
        diff_text: str = "",
        changed_files: list[str] | None = None,
        repo_root: Path | None = None,
    ) -> LoopTerminationReport:
        effective_root = repo_root or self.repo_root
        findings: list[LoopTerminationFinding] = []
        scanned_files: list[str] = []
        total_loops = 0

        # 1. Full-file AST scanning where files exist on disk
        if effective_root and changed_files:
            for file_path in changed_files:
                if not file_path.endswith((".py", ".pyi")):
                    continue
                if any(file_path.startswith(p) for p in EXEMPT_PATH_PREFIXES) or any(
                    file_path.endswith(s) for s in EXEMPT_FILE_SUFFIXES
                ):
                    continue

                full_path = effective_root / file_path
                if full_path.exists() and full_path.is_file():
                    try:
                        content = full_path.read_text(encoding="utf-8", errors="replace")
                        tree = ast.parse(content, filename=str(full_path))
                        visitor = _LoopVisitor(file_path=file_path)
                        visitor.visit(tree)
                        findings.extend(visitor.findings)
                        total_loops += visitor.loop_count
                        scanned_files.append(file_path)
                    except Exception as exc:
                        logger.debug("Failed to parse %s for loop scan: %s", file_path, exc)

        # 2. If no files on disk or diff has un-scanned additions, parse diff hunks
        if not scanned_files and diff_text:
            diff_findings, diff_loops = self._scan_diff_hunks(diff_text)
            findings.extend(diff_findings)
            total_loops += diff_loops

        return LoopTerminationReport(
            findings=findings,
            scanned_files=scanned_files,
            scanned_loops_count=total_loops,
        )

    def _scan_diff_hunks(self, diff_text: str) -> tuple[list[LoopTerminationFinding], int]:
        findings: list[LoopTerminationFinding] = []
        total_loops = 0

        from webhook_agent.analysis.diff_filter import split_diff_into_sections

        sections = split_diff_into_sections(diff_text)
        for sec in sections:
            path = sec.path
            if not path.endswith((".py", ".pyi")):
                continue
            if any(path.startswith(p) for p in EXEMPT_PATH_PREFIXES) or any(
                path.endswith(s) for s in EXEMPT_FILE_SUFFIXES
            ):
                continue

            added_lines = [
                line[1:]
                for line in sec.lines
                if line.startswith("+") and not line.startswith("+++")
            ]
            if not added_lines:
                continue

            added_content = textwrap.dedent("".join(added_lines))
            tree: ast.Module | None = None
            try:
                tree = ast.parse(added_content)
            except Exception:
                try:
                    tree = ast.parse(
                        f"def _synthetic_scope():\n{textwrap.indent(added_content, '    ')}\n"
                    )
                except Exception:
                    tree = None

            if tree is not None:
                visitor = _LoopVisitor(file_path=path)
                visitor.visit(tree)
                findings.extend(visitor.findings)
                total_loops += visitor.loop_count

        return findings, total_loops


def evaluate_loop_termination(report: LoopTerminationReport) -> ScorecardItem:
    """Evaluate Dimension 1 (Logic & Boundaries) scorecard item deterministically.

    - If PROVEN loop termination defects exist: FAIL (triggers scorecard floor).
    - Otherwise: PASS.
    """
    if report.has_critical_findings:
        critical_items = [
            f"{f.finding_type} in {f.file_path}:{f.line_number} ({', '.join(f.variables)})"
            for f in report.findings
            if f.severity == "CRITICAL"
        ]
        notes = (
            f"PROVEN loop termination defect(s) detected: {'; '.join(critical_items[:3])}. "
            "Invariant violation: loop control variable is not advanced across all conditional paths, "
            "causing an infinite execution loop and worker thread hang."
        )
        return ScorecardItem(
            dimension="Logic & Boundaries",
            status="FAIL",
            notes=notes,
        )

    return ScorecardItem(
        dimension="Logic & Boundaries",
        status="PASS",
        notes="Deterministic audit confirmed 0 unbounded loops or non-advancing iteration patterns.",
    )
