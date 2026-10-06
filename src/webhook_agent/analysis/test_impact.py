"""Smart Test Impact Analyzer (TestImpactAnalyzer).

Statically analyzes modified symbols from PR diffs, locates corresponding unit
test files in tests/, scans AST test definitions to verify test coverage, and
generates clinical test stubs for uncovered functions.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from webhook_agent.analysis.symbol_graph import (
    SignatureSpec,
    SymbolDefinitionVisitor,
)

logger = logging.getLogger("webhook_agent.analysis.test_impact")


class TestCoverageStatus(StrEnum):
    """Coverage classification for a modified symbol."""

    __test__ = False

    COVERED = "COVERED"
    UNCOVERED = "UNCOVERED"
    TEST_MODIFIED = "TEST_MODIFIED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class SymbolTestCoverage:
    """Coverage inspection details for an individual symbol."""

    symbol_name: str
    qualname: str
    source_file: str
    line_number: int
    status: TestCoverageStatus
    covering_tests: list[str] = field(default_factory=list)
    candidate_test_files: list[str] = field(default_factory=list)
    suggested_stub: str | None = None


@dataclass(frozen=True)
class TestImpactReport:
    """Clinical summary of test coverage across all modified symbols in a PR."""

    __test__ = False

    modified_source_files: list[str]
    modified_test_files: list[str]
    coverages: list[SymbolTestCoverage]

    @property
    def has_uncovered_symbols(self) -> bool:
        """Check if any modified symbol is missing test coverage."""
        return any(c.status == TestCoverageStatus.UNCOVERED for c in self.coverages)

    @property
    def covered_count(self) -> int:
        """Count of symbols verified as covered by unit tests."""
        return sum(1 for c in self.coverages if c.status == TestCoverageStatus.COVERED)

    @property
    def uncovered_count(self) -> int:
        """Count of symbols lacking unit tests."""
        return sum(1 for c in self.coverages if c.status == TestCoverageStatus.UNCOVERED)

    def to_markdown(self) -> str:
        """Render clinical Markdown report suitable for pre-compiler prompt injection."""
        lines: list[str] = ["### 🧪 Deterministic Test Coverage & Regression Impact\n"]

        if self.modified_test_files:
            test_list = ", ".join(f"`{Path(f).name}`" for f in self.modified_test_files)
            lines.append(f"**PR Modified Tests**: {test_list}\n")

        if not self.coverages:
            lines.append(
                "No callable Python functions or methods detected in modified source files."
            )
            return "\n".join(lines)

        lines.append("| Symbol | Source File | Status | Covering Test Cases |")
        lines.append("| :--- | :--- | :--- | :--- |")

        for cov in self.coverages:
            loc = f"`{cov.source_file}:{cov.line_number}`"
            sym = f"`{cov.qualname}`"
            if cov.status == TestCoverageStatus.COVERED:
                status_badge = "✅ COVERED"
                tests_str = "<br>".join(f"`{t}`" for t in cov.covering_tests[:3])
                if len(cov.covering_tests) > 3:
                    tests_str += f"<br>*(+{len(cov.covering_tests) - 3} more)*"
            elif cov.status == TestCoverageStatus.TEST_MODIFIED:
                status_badge = "📝 TEST MODIFIED"
                tests_str = "Test suite modified in PR"
            elif cov.status == TestCoverageStatus.UNCOVERED:
                status_badge = "⚠️ UNCOVERED"
                candidates = (
                    ", ".join(f"`{Path(c).name}`" for c in cov.candidate_test_files[:2])
                    if cov.candidate_test_files
                    else "None found"
                )
                tests_str = f"Missing (candidates: {candidates})"
            else:
                status_badge = "⏭️ SKIPPED"
                tests_str = "Private/Dunder symbol"

            lines.append(f"| {sym} | {loc} | {status_badge} | {tests_str} |")

        uncovered = [c for c in self.coverages if c.status == TestCoverageStatus.UNCOVERED]
        if uncovered:
            lines.append("\n#### 📋 Recommended Unit Test Stubs")
            lines.append(
                "The following symbols lack test references. Consider adding unit tests before merge:\n"
            )
            for item in uncovered[:3]:
                if item.suggested_stub:
                    lines.append(f"**`{item.qualname}`** (`{item.source_file}`):")
                    lines.append("```python")
                    lines.append(item.suggested_stub.strip())
                    lines.append("```\n")

        return "\n".join(lines)


class TestVisitor(ast.NodeVisitor):
    """AST visitor to locate test functions and identify referenced symbols."""

    def __init__(self, test_file_rel: str) -> None:
        self.test_file_rel = test_file_rel
        self.test_functions: dict[str, set[str]] = {}
        self._current_test_name: str | None = None
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._process_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._process_func(node)

    def _process_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name.startswith("test_") or (self._class_stack and "Test" in self._class_stack[-1]):
            prefix = f"{self._class_stack[-1]}." if self._class_stack else ""
            test_qual = f"{prefix}{node.name}"
            test_citation = f"{self.test_file_rel}::{test_qual}"
            prev_test = self._current_test_name
            self._current_test_name = test_citation
            self.test_functions[test_citation] = set()
            self.generic_visit(node)
            self._current_test_name = prev_test
        else:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self._current_test_name and isinstance(node.ctx, ast.Load):
            self.test_functions[self._current_test_name].add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._current_test_name and isinstance(node.ctx, ast.Load):
            self.test_functions[self._current_test_name].add(node.attr)
        self.generic_visit(node)


class TestImpactAnalyzer:
    """Analyzes test coverage impact for symbols in modified files."""

    __test__ = False

    def __init__(self, repo_root: Path | str | None = None) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        self._test_cache: dict[str, dict[str, set[str]]] = {}

    def analyze(self, changed_files: list[str]) -> TestImpactReport:
        """Analyze test coverage for all modified symbols across changed files."""
        modified_sources = [
            f for f in changed_files if f.endswith(".py") and not self._is_test_file(f)
        ]
        modified_tests = [f for f in changed_files if f.endswith(".py") and self._is_test_file(f)]

        coverages: list[SymbolTestCoverage] = []

        for src_file in modified_sources:
            full_src_path = self.repo_root / src_file
            if not full_src_path.exists():
                continue

            try:
                content = full_src_path.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(full_src_path))
            except Exception as parse_err:
                logger.debug("Could not parse source %s: %s", src_file, parse_err)
                continue

            visitor = SymbolDefinitionVisitor(file_path=src_file)
            visitor.visit(tree)

            candidate_tests = self._find_candidate_test_files(src_file)

            for _qual, sig in visitor.signatures.items():
                cov = self._inspect_symbol_coverage(
                    sig=sig,
                    src_file=src_file,
                    candidate_test_files=candidate_tests,
                    modified_tests=modified_tests,
                )
                coverages.append(cov)

        return TestImpactReport(
            modified_source_files=modified_sources,
            modified_test_files=modified_tests,
            coverages=coverages,
        )

    def _is_test_file(self, file_path: str) -> bool:
        """Check whether a file path belongs to the test suite."""
        p = Path(file_path)
        return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith("_test.py")

    def _find_candidate_test_files(self, src_file: str) -> list[str]:
        """Find candidate test files corresponding to a source file by convention."""
        src_path = Path(src_file)
        stem = src_path.stem
        candidates: list[str] = []

        exact_name = f"test_{stem}.py"

        # 1. Check mirrored path under tests/unit/
        rel_parts = list(src_path.parts)
        if rel_parts and rel_parts[0] in ("src", "lib"):
            rel_parts = rel_parts[1:]

        # Check domain-mirrored path (stripping top package e.g. webhook_agent/models -> tests/unit/models)
        if rel_parts and rel_parts[0] == "webhook_agent":
            sub_parts = rel_parts[1:]
            domain_dir = Path("tests/unit").joinpath(*sub_parts[:-1])
            domain_file = domain_dir / exact_name
            if (self.repo_root / domain_file).exists():
                candidates.append(str(domain_file))

        mirrored_dir = Path("tests/unit").joinpath(*rel_parts[:-1])
        mirrored_file = mirrored_dir / exact_name
        if (self.repo_root / mirrored_file).exists() and str(mirrored_file) not in candidates:
            candidates.append(str(mirrored_file))

        # 2. Check root tests/unit/
        unit_root_path = Path("tests/unit") / exact_name
        if (self.repo_root / unit_root_path).exists() and str(unit_root_path) not in candidates:
            candidates.append(str(unit_root_path))

        # 3. Glob match if neither exists
        if not candidates:
            tests_dir = self.repo_root / "tests"
            if tests_dir.exists():
                for match in tests_dir.rglob(exact_name):
                    try:
                        rel = str(match.relative_to(self.repo_root))
                        if rel not in candidates:
                            candidates.append(rel)
                    except ValueError:
                        pass

        return candidates

    def _get_test_functions_for_file(self, test_rel_path: str) -> dict[str, set[str]]:
        """Extract and cache test citations and referenced symbols for a test file."""
        if test_rel_path in self._test_cache:
            return self._test_cache[test_rel_path]

        full_path = self.repo_root / test_rel_path
        if not full_path.exists():
            return {}

        try:
            content = full_path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(full_path))
            visitor = TestVisitor(test_file_rel=test_rel_path)
            visitor.visit(tree)
            self._test_cache[test_rel_path] = visitor.test_functions
            return visitor.test_functions
        except Exception as exc:
            logger.debug("Failed to inspect test file %s: %s", test_rel_path, exc)
            return {}

    def _inspect_symbol_coverage(
        self,
        sig: SignatureSpec,
        src_file: str,
        candidate_test_files: list[str],
        modified_tests: list[str],
    ) -> SymbolTestCoverage:
        """Inspect whether a symbol has test coverage in candidate or repository tests."""
        sym_name = sig.symbol_name

        # Skip private or dunder methods from coverage mandates
        if sym_name.startswith("__") and sym_name.endswith("__"):
            return SymbolTestCoverage(
                symbol_name=sym_name,
                qualname=sig.qualname,
                source_file=src_file,
                line_number=sig.line_number,
                status=TestCoverageStatus.SKIPPED,
            )

        covering_tests: list[str] = []

        # 1. Check candidate test files first
        for test_file in candidate_test_files:
            test_funcs = self._get_test_functions_for_file(test_file)
            for test_citation, symbols in test_funcs.items():
                if sym_name in symbols:
                    covering_tests.append(test_citation)

        # 2. Check any modified test files in the PR
        for mod_test in modified_tests:
            if mod_test not in candidate_test_files:
                test_funcs = self._get_test_functions_for_file(mod_test)
                for test_citation, symbols in test_funcs.items():
                    if sym_name in symbols and test_citation not in covering_tests:
                        covering_tests.append(test_citation)

        if covering_tests:
            return SymbolTestCoverage(
                symbol_name=sym_name,
                qualname=sig.qualname,
                source_file=src_file,
                line_number=sig.line_number,
                status=TestCoverageStatus.COVERED,
                covering_tests=covering_tests,
                candidate_test_files=candidate_test_files,
            )

        # 3. If test file itself was modified, check if it's considered covered
        for cand in candidate_test_files:
            if cand in modified_tests:
                return SymbolTestCoverage(
                    symbol_name=sym_name,
                    qualname=sig.qualname,
                    source_file=src_file,
                    line_number=sig.line_number,
                    status=TestCoverageStatus.TEST_MODIFIED,
                    candidate_test_files=candidate_test_files,
                )

        # 4. Symbol is uncovered -> Generate a clinical test stub
        stub = self._generate_test_stub(sig, src_file)
        return SymbolTestCoverage(
            symbol_name=sym_name,
            qualname=sig.qualname,
            source_file=src_file,
            line_number=sig.line_number,
            status=TestCoverageStatus.UNCOVERED,
            candidate_test_files=candidate_test_files,
            suggested_stub=stub,
        )

    def _generate_test_stub(self, sig: SignatureSpec, src_file: str) -> str:
        """Generate a pytest function stub tailored to the symbol signature."""
        sym_name = sig.symbol_name
        test_fn_name = f"test_{sym_name}_basic"

        # Determine import target
        src_path = Path(src_file)
        parts = list(src_path.parts)
        if parts and parts[0] in ("src", "lib"):
            parts = parts[1:]
        mod_dotted = ".".join(parts).removesuffix(".py")

        args_setup: list[str] = []
        call_args: list[str] = []

        params = sig.parameters
        if sig.is_method and sig.first_arg_is_self and params:
            params = params[1:]

        for p in params:
            if p.kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
                continue
            if not p.has_default:
                default_val = "MagicMock()"
                if p.annotation:
                    ann = p.annotation.lower()
                    if "str" in ann:
                        default_val = f'"{p.name}_sample"'
                    elif "int" in ann:
                        default_val = "1"
                    elif "bool" in ann:
                        default_val = "True"
                    elif "list" in ann:
                        default_val = "[]"
                    elif "dict" in ann:
                        default_val = "{}"
                args_setup.append(f"    mock_{p.name} = {default_val}")
                call_args.append(f"mock_{p.name}")

        setup_code = "\n".join(args_setup)

        args_str = ", ".join(call_args)
        if sig.is_method:
            class_name = sig.qualname.split(".")[0]
            call_code = f"instance.{sym_name}({args_str})"
            lines = [
                f"def {test_fn_name}():",
                f'    """Verify {sig.qualname} behavior."""',
                f"    from {mod_dotted} import {class_name}",
                f"    instance = {class_name}()",
            ]
            if setup_code:
                lines.append(setup_code)
            lines.extend(
                [
                    f"    result = {call_code}",
                    "    assert result is not None",
                ]
            )
        else:
            call_code = f"{sym_name}({args_str})"
            lines = [
                f"def {test_fn_name}():",
                f'    """Verify {sym_name} behavior."""',
                f"    from {mod_dotted} import {sym_name}",
            ]
            if setup_code:
                lines.append(setup_code)
            lines.extend(
                [
                    f"    result = {call_code}",
                    "    assert result is not None",
                ]
            )

        return "\n".join(lines)
