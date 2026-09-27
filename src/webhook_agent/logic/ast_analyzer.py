"""AST-Aware Diff Chunk Analyzer for Python code review and risk scoring."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Literal

ASTImpactCategory = Literal["BREAKING_SIGNATURE", "CLASS_INHERITANCE", "INTERNAL_LOGIC", "DOCS_OR_COMMENTS"]

IMPACT_WEIGHTS = {
    "BREAKING_SIGNATURE": 1.0,
    "CLASS_INHERITANCE": 0.8,
    "INTERNAL_LOGIC": 0.4,
    "DOCS_OR_COMMENTS": 0.1,
}

@dataclass
class ASTAnalysisResult:
    impact_counts: dict[str, int] = field(default_factory=lambda: {
        "BREAKING_SIGNATURE": 0,
        "CLASS_INHERITANCE": 0,
        "INTERNAL_LOGIC": 0,
        "DOCS_OR_COMMENTS": 0,
    })
    risk_score: float = 0.0
    nodes_analyzed: int = 0
    categories_found: list[str] = field(default_factory=list)


class ASTImpactVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.counts: dict[str, int] = {
            "BREAKING_SIGNATURE": 0,
            "CLASS_INHERITANCE": 0,
            "INTERNAL_LOGIC": 0,
            "DOCS_OR_COMMENTS": 0,
        }
        self.nodes_analyzed = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.counts["BREAKING_SIGNATURE"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.counts["BREAKING_SIGNATURE"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.bases:
            self.counts["CLASS_INHERITANCE"] += 1
        else:
            self.counts["BREAKING_SIGNATURE"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        self.nodes_analyzed += 1
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self.counts["DOCS_OR_COMMENTS"] += 1
        else:
            self.counts["INTERNAL_LOGIC"] += 1
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.counts["INTERNAL_LOGIC"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.counts["INTERNAL_LOGIC"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.counts["INTERNAL_LOGIC"] += 1
        self.nodes_analyzed += 1
        self.generic_visit(node)


def analyze_python_code(code_text: str) -> ASTAnalysisResult:
    """Parse and analyze Python code text using AST to categorize changes and compute risk score."""
    if not code_text or not code_text.strip():
        return ASTAnalysisResult()

    try:
        tree = ast.parse(code_text)
    except SyntaxError:
        try:
            tree = ast.parse("\n".join(line for line in code_text.splitlines() if not line.startswith("-")))
        except SyntaxError:
            return ASTAnalysisResult(risk_score=0.2, nodes_analyzed=0, categories_found=["DOCS_OR_COMMENTS"])

    visitor = ASTImpactVisitor()
    visitor.visit(tree)

    total_nodes = visitor.nodes_analyzed
    if total_nodes == 0:
        return ASTAnalysisResult()

    weighted_sum = sum(
        visitor.counts[cat] * IMPACT_WEIGHTS[cat]
        for cat in visitor.counts
    )
    risk_score = min(1.0, max(0.0, weighted_sum / max(1, total_nodes)))
    if visitor.counts["BREAKING_SIGNATURE"] > 0:
        risk_score = max(risk_score, 0.6)
    if visitor.counts["CLASS_INHERITANCE"] > 0:
        risk_score = max(risk_score, 0.5)

    categories_found = [cat for cat, count in visitor.counts.items() if count > 0]

    return ASTAnalysisResult(
        impact_counts=visitor.counts,
        risk_score=round(risk_score, 3),
        nodes_analyzed=total_nodes,
        categories_found=categories_found,
    )


def analyze_diff_hunks(diff_text: str) -> ASTAnalysisResult:
    """Extract added/modified lines from Python diff text and analyze via AST."""
    if not diff_text or not diff_text.strip():
        return ASTAnalysisResult()

    python_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            python_lines.append(line[1:])
        elif not line.startswith("-") and not line.startswith("@@") and not line.startswith("diff"):
            python_lines.append(line)

    code_block = "\n".join(python_lines)
    return analyze_python_code(code_block)
