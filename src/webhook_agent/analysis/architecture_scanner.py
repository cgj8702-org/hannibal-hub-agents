"""Deterministic Architectural Layer & Boundary Scanner (ArchitecturalBoundaryScanner).

Enforces unidirectional package layering and isolation across Hannibal Hub Agents.
Prevents low-level offline modules (analysis, models, state) from importing high-level
interactive runtime tools, orchestrators, or circular dependencies.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.architecture_scanner")

# Forbidden import prefixes per package under src/webhook_agent/
ARCHITECTURAL_RULES: dict[str, tuple[str, ...]] = {
    "analysis": (
        "webhook_agent.tools",
        "webhook_agent.core",
        "webhook_agent.processor",
        "webhook_agent.worker",
        "webhook_agent.environment",
        "webhook_agent.events",
        "google.adk",
    ),
    "models": (
        "webhook_agent.tools",
        "webhook_agent.processor",
        "webhook_agent.worker",
        "webhook_agent.core.execution",
        "webhook_agent.environment",
    ),
    "state": (
        "webhook_agent.tools",
        "webhook_agent.processor",
        "webhook_agent.worker",
        "webhook_agent.core.execution",
        "webhook_agent.environment",
    ),
    "review": (
        "webhook_agent.processor",
        "webhook_agent.worker",
        "webhook_agent.environment",
    ),
    "tools": (
        "webhook_agent.processor",
        "webhook_agent.worker",
        "webhook_agent.core.execution",
    ),
    "core": (
        "webhook_agent.processor",
        "webhook_agent.worker",
    ),
}

EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "tests/",
    "test/",
    "testing/",
    "fixtures/",
    "test_fixtures/",
)


@dataclass(frozen=True)
class ArchitecturalFinding:
    """An individual architectural boundary violation detected in source code."""

    file_path: str
    line_number: int
    source_package: str
    imported_module: str
    prohibited_prefix: str
    description: str
    severity: Literal["CRITICAL", "WARN"] = "CRITICAL"


@dataclass(frozen=True)
class ArchitecturalReport:
    """Clinical summary of architectural layer integrity and boundary scans."""

    findings: list[ArchitecturalFinding] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)

    @property
    def has_critical_findings(self) -> bool:
        """Return True if any critical architectural boundary violations were detected."""
        return any(f.severity == "CRITICAL" for f in self.findings)

    def to_markdown(self, **kwargs: Any) -> str:
        """Render clinical Markdown summary of architectural findings."""
        if not self.findings:
            return "Deterministic audit confirmed architectural layering and package boundary integrity."

        lines: list[str] = [
            f"Found {len(self.findings)} architectural boundary violation(s) across {len(self.scanned_files)} file(s):"
        ]
        for f in self.findings:
            loc = f"`{f.file_path}:{f.line_number}`"
            lines.append(
                f"- 🔴 **[BOUNDARY_INVERSION]** {loc} in package `{f.source_package}`: {f.description}"
            )
        return "\n".join(lines)


class _ArchitectureVisitor(ast.NodeVisitor):
    """AST Visitor detecting prohibited runtime imports in restricted packages."""

    def __init__(self, file_path: str, source_package: str) -> None:
        self.file_path = file_path
        self.source_package = source_package
        self.prohibited_prefixes = ARCHITECTURAL_RULES.get(source_package, ())
        self.findings: list[ArchitecturalFinding] = []
        self._in_type_checking = False

    def visit_If(self, node: ast.If) -> None:
        was_type_checking = self._in_type_checking
        if self._is_type_checking_test(node.test):
            self._in_type_checking = True
            for stmt in node.body:
                self.visit(stmt)
            self._in_type_checking = was_type_checking
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def _is_type_checking_test(self, test: ast.expr) -> bool:
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_type_checking:
            return
        for alias in node.names:
            self._check_import_module(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._in_type_checking:
            return
        # Resolve full imported module name
        resolved_module = self._resolve_import_from(node)
        if resolved_module:
            self._check_import_module(resolved_module, node.lineno)

    def _resolve_import_from(self, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        # Relative import
        if node.level == 1:
            # from .foo import bar -> webhook_agent.<package>.foo
            mod = f".{node.module}" if node.module else ""
            return f"webhook_agent.{self.source_package}{mod}"
        if node.level == 2:
            # from ..tools import bar -> webhook_agent.tools
            mod = f".{node.module}" if node.module else ""
            return f"webhook_agent{mod}"
        return node.module or ""

    def _check_import_module(self, module_name: str, lineno: int) -> None:
        for prefix in self.prohibited_prefixes:
            if module_name == prefix or module_name.startswith(f"{prefix}."):
                self.findings.append(
                    ArchitecturalFinding(
                        file_path=self.file_path,
                        line_number=lineno,
                        source_package=self.source_package,
                        imported_module=module_name,
                        prohibited_prefix=prefix,
                        description=(
                            f"Prohibited import `{module_name}` in package `{self.source_package}`. "
                            f"Violates layer hierarchy rule prohibiting `{prefix}` dependencies in `{self.source_package}`."
                        ),
                        severity="CRITICAL",
                    )
                )


def _get_package_from_path(file_path: str) -> str | None:
    """Extract canonical subpackage name under src/webhook_agent/."""
    path_obj = Path(file_path)
    parts = path_obj.parts
    # Look for 'webhook_agent' in path
    if "webhook_agent" in parts:
        idx = parts.index("webhook_agent")
        if idx + 1 < len(parts) - 1:
            return parts[idx + 1]
    return None


class ArchitecturalBoundaryScanner:
    """Scanner analyzing Python files and diffs for architectural boundary inversions."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root

    def scan(
        self,
        diff_text: str = "",
        changed_files: list[str] | None = None,
        repo_root: Path | None = None,
    ) -> ArchitecturalReport:
        effective_root = repo_root or self.repo_root
        findings: list[ArchitecturalFinding] = []
        scanned_files: list[str] = []

        # 1. Full-file AST scanning on disk
        if effective_root and changed_files:
            for file_path in changed_files:
                if not file_path.endswith((".py", ".pyi")):
                    continue
                if any(file_path.startswith(p) for p in EXEMPT_PATH_PREFIXES):
                    continue

                pkg = _get_package_from_path(file_path)
                if not pkg or pkg not in ARCHITECTURAL_RULES:
                    continue

                full_path = effective_root / file_path
                if full_path.exists() and full_path.is_file():
                    try:
                        content = full_path.read_text(encoding="utf-8", errors="replace")
                        tree = ast.parse(content, filename=str(full_path))
                        visitor = _ArchitectureVisitor(file_path=file_path, source_package=pkg)
                        visitor.visit(tree)
                        findings.extend(visitor.findings)
                        scanned_files.append(file_path)
                    except Exception as exc:
                        logger.debug("Failed to parse %s for architecture scan: %s", file_path, exc)

        # 2. Diff hunk scanning if files not on disk
        if not scanned_files and diff_text:
            diff_findings = self._scan_diff_hunks(diff_text)
            findings.extend(diff_findings)

        return ArchitecturalReport(
            findings=findings,
            scanned_files=scanned_files,
        )

    def _scan_diff_hunks(self, diff_text: str) -> list[ArchitecturalFinding]:
        findings: list[ArchitecturalFinding] = []
        from webhook_agent.analysis.diff_filter import split_diff_into_sections

        sections = split_diff_into_sections(diff_text)
        for sec in sections:
            path = sec.path
            if not path.endswith((".py", ".pyi")):
                continue
            if any(path.startswith(p) for p in EXEMPT_PATH_PREFIXES):
                continue

            pkg = _get_package_from_path(path)
            if not pkg or pkg not in ARCHITECTURAL_RULES:
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
                visitor = _ArchitectureVisitor(file_path=path, source_package=pkg)
                visitor.visit(tree)
                findings.extend(visitor.findings)

        return findings


def evaluate_architecture_contract(report: ArchitecturalReport) -> ScorecardItem:
    """Evaluate Dimension 4 (Contract Integrity) scorecard item deterministically.

    - If PROVEN architectural boundary inversions exist: FAIL (triggers scorecard floor).
    - Otherwise: PASS.
    """
    if report.has_critical_findings:
        summaries = [
            f"{f.imported_module} in {f.file_path}:{f.line_number} (prohibited in '{f.source_package}')"
            for f in report.findings
            if f.severity == "CRITICAL"
        ]
        notes = (
            f"PROVEN architectural boundary violation(s): {'; '.join(summaries[:3])}. "
            "Invariant violation: low-level package imports prohibited high-level runtime or tool dependency."
        )
        return ScorecardItem(
            dimension="Contract Integrity",
            status="FAIL",
            notes=notes,
        )

    return ScorecardItem(
        dimension="Contract Integrity",
        status="PASS",
        notes="Deterministic audit confirmed architectural layering and package boundary integrity.",
    )
