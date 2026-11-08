"""Deterministic AST Tautology and Mock Abuse Scanner (TautologyScanner).

Statically analyzes modified Python unit tests to detect fraudulent, vacuous,
or self-mocking tests that circumvent real code execution.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.tautology_scanner")


@dataclass(frozen=True)
class TautologyFinding:
    """An individual tautological test defect identified in test code."""

    test_file: str
    test_name: str
    line_number: int
    finding_type: str  # "SELF_MOCKING", "VACUOUS_ASSERTION", "EMPTY_TEST"
    description: str
    severity: Literal["CRITICAL", "WARN"] = "CRITICAL"


@dataclass(frozen=True)
class TautologyReport:
    """Clinical summary of detected test tautologies and mock violations."""

    findings: list[TautologyFinding] = field(default_factory=list)
    scanned_test_files: list[str] = field(default_factory=list)
    scanned_test_count: int = 0

    @property
    def has_critical_tautologies(self) -> bool:
        """Return True if any critical test fraud or self-mocking was detected."""
        return any(f.severity == "CRITICAL" for f in self.findings)

    def to_markdown(self, **kwargs: Any) -> str:
        """Render clinical Markdown report of test tautologies."""
        if not self.findings:
            return "No tautological tests or fraudulent mock patterns detected in test suite."

        lines: list[str] = [
            f"Found {len(self.findings)} tautological or vacuous test pattern(s) across {len(self.scanned_test_files)} test file(s):"
        ]
        for f in self.findings:
            badge = "🔴" if f.severity == "CRITICAL" else "🟡"
            loc = f"`{f.test_file}:{f.line_number}`"
            test_cite = f"`{f.test_name}`"
            lines.append(f"- {badge} **[{f.finding_type}]** {loc} in {test_cite}: {f.description}")
        return "\n".join(lines)


class _TestTautologyVisitor(ast.NodeVisitor):
    """AST visitor to inspect individual test functions for mock abuse and vacuous asserts."""

    def __init__(
        self,
        test_file: str,
        modified_symbols: set[str] | None = None,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> None:
        self.test_file = test_file
        self.modified_symbols = modified_symbols or set()
        self.line_ranges = line_ranges
        self.findings: list[TautologyFinding] = []
        self._current_test_name: str | None = None
        self._test_count: int = 0
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_test_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_test_func(node)

    def _check_test_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Check if function is a test function (test_* or inside a Test* class with test*)
        is_test = node.name.startswith("test")
        if not is_test:
            self.generic_visit(node)
            return

        # If line_ranges is provided, only inspect tests that overlap with modified hunks
        if self.line_ranges:
            fn_start = node.lineno
            fn_end = getattr(node, "end_lineno", node.lineno)
            overlaps = any(
                max(fn_start, h_start) <= min(fn_end, h_end) for h_start, h_end in self.line_ranges
            )
            if not overlaps:
                return

        prefix = f"{self._class_stack[-1]}." if self._class_stack else ""
        test_name = f"{prefix}{node.name}"
        self._test_count += 1
        prev_test = self._current_test_name
        self._current_test_name = test_name

        # 1. Check for empty test (only pass, docstrings, or ellipsis)
        statements = [
            s
            for s in node.body
            if not (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant)
                and isinstance(s.value.value, str)
            )
        ]
        if not statements or all(
            isinstance(s, ast.Pass)
            or (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant)
                and s.value.value is Ellipsis
            )
            for s in statements
        ):
            self.findings.append(
                TautologyFinding(
                    test_file=self.test_file,
                    test_name=test_name,
                    line_number=node.lineno,
                    finding_type="EMPTY_TEST",
                    description="Test body is empty (contains only pass, docstring, or ellipsis) and verifies no logic.",
                    severity="CRITICAL",
                )
            )
            self._current_test_name = prev_test
            return

        # 2. Inspect assertions and mocks in the test body
        asserts: list[ast.Assert] = []
        assertion_calls: list[ast.Call] = []  # self.assertEqual, self.assertTrue, etc.
        has_exception_catcher = False
        mocked_symbols: dict[str, int] = {}  # symbol_name -> lineno
        mock_assigned_vars: dict[str, str] = {}  # var_name -> symbol_name

        # Check decorators on test function for patch
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                self._check_call_mock(dec, mocked_symbols)

        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                asserts.append(child)
            elif isinstance(child, ast.With):
                for item in child.items:
                    self._check_with_mock(item.context_expr, mocked_symbols)
                    if self._is_exception_catcher(item.context_expr):
                        has_exception_catcher = True
            elif isinstance(child, ast.Call):
                self._check_call_mock(child, mocked_symbols)
                if self._is_exception_catcher(child):
                    has_exception_catcher = True
                elif self._is_unittest_assertion(child):
                    assertion_calls.append(child)
            elif isinstance(child, ast.Assign):
                self._track_mock_assign(child, mocked_symbols, mock_assigned_vars)

        total_assertions = len(asserts) + len(assertion_calls)

        # 3. Check for tests with zero assertions and no expected exception
        if total_assertions == 0 and not has_exception_catcher:
            self.findings.append(
                TautologyFinding(
                    test_file=self.test_file,
                    test_name=test_name,
                    line_number=node.lineno,
                    finding_type="EMPTY_TEST",
                    description="Test contains no assert statements and does not expect any exceptions.",
                    severity="WARN",
                )
            )

        # 4. Check for vacuous asserts (assert True, assert 1 == 1, assert x == x)
        for ass in asserts:
            self._check_vacuous_assert(ass, test_name)
        for ass_call in assertion_calls:
            self._check_vacuous_assert_call(ass_call, test_name)

        # 5. Check for self-mocking (mocking the target under test)
        for mocked_sym, mock_line in mocked_symbols.items():
            is_modified = mocked_sym in self.modified_symbols
            # Check if any assert in the test calls or checks the mocked symbol or assigned var
            calls_mocked_sym = self._assert_calls_symbol(
                asserts, assertion_calls, mocked_sym, mock_assigned_vars
            )
            if is_modified and calls_mocked_sym:
                self.findings.append(
                    TautologyFinding(
                        test_file=self.test_file,
                        test_name=test_name,
                        line_number=mock_line,
                        finding_type="SELF_MOCKING",
                        description=(
                            f"Test mocks modified target symbol '{mocked_sym}' via patch/mock "
                            "and asserts against the mocked result, providing zero genuine coverage."
                        ),
                        severity="CRITICAL",
                    )
                )
            elif calls_mocked_sym and self._is_tautological_mock_assert(
                asserts, assertion_calls, mocked_sym, mock_assigned_vars
            ):
                self.findings.append(
                    TautologyFinding(
                        test_file=self.test_file,
                        test_name=test_name,
                        line_number=mock_line,
                        finding_type="SELF_MOCKING",
                        description=(
                            f"Test mocks method '{mocked_sym}' and tautologically asserts its own mocked return value."
                        ),
                        severity="CRITICAL",
                    )
                )

        self._current_test_name = prev_test
        self.generic_visit(node)

    def _track_mock_assign(
        self,
        assign: ast.Assign,
        mocked_symbols: dict[str, int],
        mock_assigned_vars: dict[str, str],
    ) -> None:
        """Track variables assigned from calls to mocked symbols (e.g. res = limiter.is_allowed(...))."""
        val = assign.value
        if not isinstance(val, ast.Call):
            return
        func = val.func
        called_name: str | None = None
        if isinstance(func, ast.Name):
            called_name = func.id
        elif isinstance(func, ast.Attribute):
            called_name = func.attr

        if called_name and called_name in mocked_symbols:
            for tgt in assign.targets:
                if isinstance(tgt, ast.Name):
                    mock_assigned_vars[tgt.id] = called_name

    def _is_patch_object_call(self, func: ast.AST) -> bool:
        """Detect patch.object or mock.patch.object."""
        if isinstance(func, ast.Attribute) and func.attr == "object":
            val = func.value
            return (isinstance(val, ast.Name) and val.id == "patch") or (
                isinstance(val, ast.Attribute) and val.attr == "patch"
            )
        return False

    def _is_patch_call(self, func: ast.AST) -> bool:
        """Detect patch or mock.patch."""
        return (isinstance(func, ast.Name) and func.id == "patch") or (
            isinstance(func, ast.Attribute) and func.attr == "patch"
        )

    def _check_with_mock(self, expr: ast.expr, mocked_symbols: dict[str, int]) -> None:
        """Detect patch.object or patch context managers."""
        if not isinstance(expr, ast.Call):
            return
        func = expr.func
        if self._is_patch_object_call(func):
            if (
                len(expr.args) >= 2
                and isinstance(expr.args[1], ast.Constant)
                and isinstance(expr.args[1].value, str)
            ):
                mocked_symbols[expr.args[1].value] = expr.lineno
        elif self._is_patch_call(func):
            if (
                expr.args
                and isinstance(expr.args[0], ast.Constant)
                and isinstance(expr.args[0].value, str)
            ):
                target_str = expr.args[0].value
                leaf_name = target_str.split(".")[-1]
                mocked_symbols[leaf_name] = expr.lineno

    def _check_call_mock(self, call: ast.Call, mocked_symbols: dict[str, int]) -> None:
        """Detect monkeypatch.setattr or patch decorator/calls."""
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "setattr":
            if isinstance(func.value, ast.Name) and "monkeypatch" in func.value.id.lower():
                if (
                    len(call.args) >= 2
                    and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str)
                ):
                    mocked_symbols[call.args[1].value] = call.lineno
                elif (
                    len(call.args) >= 1
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    leaf_name = call.args[0].value.split(".")[-1]
                    mocked_symbols[leaf_name] = call.lineno
        elif self._is_patch_object_call(func):
            if (
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            ):
                mocked_symbols[call.args[1].value] = call.lineno
        elif self._is_patch_call(func):
            if (
                call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                leaf_name = call.args[0].value.split(".")[-1]
                mocked_symbols[leaf_name] = call.lineno

    def _is_exception_catcher(self, node: ast.AST) -> bool:
        """Check if node represents pytest.raises, pytest.warns, or assertRaises."""
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in ("raises", "warns")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "pytest"
                ):
                    return True
                if func.attr.startswith("assertRaises"):
                    return True
        return False

    def _is_unittest_assertion(self, call: ast.Call) -> bool:
        """Check if call is self.assertEqual, self.assertTrue, mock.assert_*, etc."""
        func = call.func
        if not isinstance(func, ast.Attribute):
            return False
        # self.assertEqual, self.assertTrue, self.fail, etc.
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and (func.attr.startswith("assert") or func.attr.startswith("fail"))
        ):
            return True
        # Mock assertions on any object: mock.assert_called, mock.assert_called_once,
        # mock.assert_called_with, mock.assert_called_once_with, mock.assert_not_called,
        # mock.assert_any_call, mock.assert_has_calls
        if func.attr.startswith("assert_"):
            return True
        return func.attr == "fail"

    def _check_vacuous_assert(self, ass: ast.Assert, test_name: str) -> None:
        """Detect assert True, assert 1 == 1, assert x == x."""
        test_expr = ass.test
        if isinstance(test_expr, ast.Constant):
            if bool(test_expr.value) is True:
                self.findings.append(
                    TautologyFinding(
                        test_file=self.test_file,
                        test_name=test_name,
                        line_number=ass.lineno,
                        finding_type="VACUOUS_ASSERTION",
                        description=f"Tautological assert statement: assert {test_expr.value!r} tests nothing.",
                        severity="CRITICAL",
                    )
                )
        elif isinstance(test_expr, ast.Compare):
            if len(test_expr.ops) == 1 and isinstance(test_expr.ops[0], (ast.Eq, ast.Is)):
                left_str = ast.unparse(test_expr.left).strip()
                right_str = ast.unparse(test_expr.comparators[0]).strip()
                if left_str == right_str:
                    self.findings.append(
                        TautologyFinding(
                            test_file=self.test_file,
                            test_name=test_name,
                            line_number=ass.lineno,
                            finding_type="VACUOUS_ASSERTION",
                            description=f"Tautological identity comparison: assert {left_str} == {right_str} compares an expression to itself.",
                            severity="CRITICAL",
                        )
                    )

    def _check_vacuous_assert_call(self, call: ast.Call, test_name: str) -> None:
        """Detect self.assertTrue(True), self.assertEqual(1, 1), etc."""
        func = call.func
        if not isinstance(func, ast.Attribute):
            return
        attr = func.attr
        if attr in ("assertTrue", "assertFalse") and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.Constant):
                self.findings.append(
                    TautologyFinding(
                        test_file=self.test_file,
                        test_name=test_name,
                        line_number=call.lineno,
                        finding_type="VACUOUS_ASSERTION",
                        description=f"Tautological unittest assertion: {attr}({arg.value!r}) tests nothing.",
                        severity="CRITICAL",
                    )
                )
        elif attr in ("assertEqual", "assertIs") and len(call.args) >= 2:
            left_str = ast.unparse(call.args[0]).strip()
            right_str = ast.unparse(call.args[1]).strip()
            if left_str == right_str:
                self.findings.append(
                    TautologyFinding(
                        test_file=self.test_file,
                        test_name=test_name,
                        line_number=call.lineno,
                        finding_type="VACUOUS_ASSERTION",
                        description=f"Tautological unittest assertion: {attr}({left_str}, {right_str}) compares an expression to itself.",
                        severity="CRITICAL",
                    )
                )

    def _assert_calls_symbol(
        self,
        asserts: list[ast.Assert],
        assertion_calls: list[ast.Call],
        symbol_name: str,
        mock_assigned_vars: dict[str, str],
    ) -> bool:
        """Return True if any assertion checks the mocked symbol or a variable assigned from it."""
        vars_for_sym = {var for var, sym in mock_assigned_vars.items() if sym == symbol_name}

        for ass in asserts:
            for child in ast.walk(ass):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name) and child.func.id == symbol_name:
                        return True
                    if isinstance(child.func, ast.Attribute) and child.func.attr == symbol_name:
                        return True
                elif isinstance(child, ast.Name) and child.id in vars_for_sym:
                    return True

        for ass_call in assertion_calls:
            for child in ast.walk(ass_call):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name) and child.func.id == symbol_name:
                        return True
                    if isinstance(child.func, ast.Attribute) and child.func.attr == symbol_name:
                        return True
                elif isinstance(child, ast.Name) and child.id in vars_for_sym:
                    return True

        return False

    def _is_tautological_mock_assert(
        self,
        asserts: list[ast.Assert],
        assertion_calls: list[ast.Call],
        symbol_name: str,
        mock_assigned_vars: dict[str, str],
    ) -> bool:
        """Check if all assertions directly verify the mocked symbol with no other real assertions."""
        vars_for_sym = {var for var, sym in mock_assigned_vars.items() if sym == symbol_name}
        has_mock_assert = False
        has_other_real_assert = False

        for ass in asserts:
            calls_mock = False
            for child in ast.walk(ass):
                if isinstance(child, ast.Call):
                    if (isinstance(child.func, ast.Name) and child.func.id == symbol_name) or (
                        isinstance(child.func, ast.Attribute) and child.func.attr == symbol_name
                    ):
                        calls_mock = True
                        break
                elif isinstance(child, ast.Name) and child.id in vars_for_sym:
                    calls_mock = True
                    break
            if calls_mock:
                has_mock_assert = True
            else:
                has_other_real_assert = True

        for ass_call in assertion_calls:
            calls_mock = False
            for child in ast.walk(ass_call):
                if isinstance(child, ast.Call):
                    if (isinstance(child.func, ast.Name) and child.func.id == symbol_name) or (
                        isinstance(child.func, ast.Attribute) and child.func.attr == symbol_name
                    ):
                        calls_mock = True
                        break
                elif isinstance(child, ast.Name) and child.id in vars_for_sym:
                    calls_mock = True
                    break
            if calls_mock:
                has_mock_assert = True
            else:
                has_other_real_assert = True

        return has_mock_assert and not has_other_real_assert


class TautologyScanner:
    """Production scanner detecting test tautologies, empty tests, and self-mocking fraud."""

    def __init__(self, repo_root: Path | str | None = None) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()

    def scan_code(
        self,
        code: str,
        file_path: str = "test_sample.py",
        modified_symbols: set[str] | None = None,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> list[TautologyFinding]:
        """Scan Python code text for tautological test patterns."""
        if not code or not code.strip():
            return []
        try:
            tree = ast.parse(code, filename=file_path)
            visitor = _TestTautologyVisitor(
                test_file=file_path,
                modified_symbols=modified_symbols,
                line_ranges=line_ranges,
            )
            visitor.visit(tree)
            return visitor.findings
        except Exception as exc:
            logger.debug("Failed parsing test code %s for tautology scan: %s", file_path, exc)
            return []

    def scan_file(
        self,
        file_path: Path | str,
        modified_symbols: set[str] | None = None,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> list[TautologyFinding]:
        """Scan a test file on disk for tautological test patterns."""
        p = Path(file_path)
        abs_path = p if p.is_absolute() else (self.repo_root / p)
        if not abs_path.is_file():
            return []
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            rel_str = (
                str(abs_path.relative_to(self.repo_root))
                if abs_path.is_relative_to(self.repo_root)
                else str(p)
            )
            return self.scan_code(
                content,
                file_path=rel_str,
                modified_symbols=modified_symbols,
                line_ranges=line_ranges,
            )
        except Exception as exc:
            logger.debug("Failed reading test file %s: %s", file_path, exc)
            return []

    def scan(
        self,
        test_files: list[str],
        modified_symbols: set[str] | None = None,
    ) -> TautologyReport:
        """Scan a list of test files for tautologies."""
        findings: list[TautologyFinding] = []
        scanned_files: list[str] = []

        for tf in test_files:
            file_findings = self.scan_file(tf, modified_symbols=modified_symbols)
            if file_findings:
                findings.extend(file_findings)
            scanned_files.append(tf)

        return TautologyReport(
            findings=findings,
            scanned_test_files=scanned_files,
            scanned_test_count=len(scanned_files),
        )

    def scan_diff(
        self,
        diff_text: str,
        modified_symbols: set[str] | None = None,
    ) -> TautologyReport:
        """Scan diff text directly to extract modified test file contents and audit them."""
        if not diff_text or not diff_text.strip():
            return TautologyReport()

        from webhook_agent.analysis.diff_filter import (
            extract_hunk_ranges,
            split_diff_into_sections,
        )

        sections = split_diff_into_sections(diff_text)
        hunk_ranges_by_file = extract_hunk_ranges(diff_text)
        findings: list[TautologyFinding] = []
        scanned_files: list[str] = []

        for sec in sections:
            path = sec.path
            is_test = (
                "tests" in path or Path(path).name.startswith("test_") or path.endswith("_test.py")
            )
            if not is_test or not path.endswith(".py"):
                continue

            scanned_files.append(path)
            file_ranges = hunk_ranges_by_file.get(path, [])
            abs_file = self.repo_root / path

            # Priority 1: If file exists on disk, scan real file scoped strictly to the modified hunk ranges
            if abs_file.is_file():
                file_findings = self.scan_file(
                    path,
                    modified_symbols=modified_symbols,
                    line_ranges=file_ranges or None,
                )
                if file_findings:
                    findings.extend(file_findings)
                continue

            # Priority 2: Fallback for in-memory or synthetic diffs without a working copy on disk
            added_lines = [
                line[1:]
                for line in sec.lines
                if line.startswith("+") and not line.startswith("+++")
            ]
            added_content = textwrap.dedent("".join(added_lines))

            # Try parsing added content; if partial snippet, wrap in test function if needed
            file_findings = self.scan_code(
                added_content,
                file_path=path,
                modified_symbols=modified_symbols,
            )
            if not file_findings and added_content.strip():
                # If snippet had no function def (e.g. lines added inside existing test), wrap and check
                synthetic = (
                    f"def test_synthetic_diff_scope():\n{textwrap.indent(added_content, '    ')}\n"
                )
                file_findings = self.scan_code(
                    synthetic,
                    file_path=path,
                    modified_symbols=modified_symbols,
                )

            if file_findings:
                findings.extend(file_findings)

        return TautologyReport(
            findings=findings,
            scanned_test_files=scanned_files,
            scanned_test_count=len(scanned_files),
        )


def evaluate_tautology_integrity(
    report: TautologyReport,
) -> ScorecardItem:
    """Evaluate Dimension 5 (Test Coverage & Regressions) impact from tautology report."""
    if report.has_critical_tautologies:
        findings_str = "; ".join(
            f"{f.test_name}: {f.description}" for f in report.findings if f.severity == "CRITICAL"
        )
        return ScorecardItem(
            dimension="Test Coverage & Regressions",
            status="FAIL",
            notes=f"CRITICAL: Tautological test defect(s) detected: {findings_str}",
        )

    if report.findings:
        findings_str = "; ".join(f"{f.test_name}: {f.description}" for f in report.findings[:3])
        return ScorecardItem(
            dimension="Test Coverage & Regressions",
            status="WARN",
            notes=f"Suspicious test pattern(s) identified: {findings_str}",
        )

    return ScorecardItem(
        dimension="Test Coverage & Regressions",
        status="PASS",
        notes="No tautological tests or self-mocking detected in test suite.",
    )
