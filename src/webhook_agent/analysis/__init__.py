"""Static code analysis, Python AST parsing, symbol graphs, and test impact evaluation."""

from __future__ import annotations

from .ast_analyzer import ASTAnalysisResult, analyze_diff_hunks, analyze_python_code
from .dependency_tree import (
    LockfilePackage,
    build_dependency_grounding_context,
    get_transitive_closure,
    parse_lockfile_dependency_graph,
)
from .diff_filter import filter_review_diff, skip_reason
from .lockfile_validator import (
    LockfileValidationResult,
    is_pure_dependency_pr,
    parse_bumped_packages_from_diff,
    render_deterministic_approval_markdown,
    validate_lockfile_diff,
)
from .symbol_graph import (
    BrokenCallSite,
    CallSiteSpec,
    CallSiteVisitor,
    ParameterSpec,
    SignatureDiff,
    SignatureSpec,
    SymbolDefinitionVisitor,
    SymbolImpactAnalyzer,
    SymbolImpactReport,
)
from .test_impact import TestImpactAnalyzer, TestImpactReport

__all__ = [
    "ASTAnalysisResult",
    "BrokenCallSite",
    "CallSiteSpec",
    "CallSiteVisitor",
    "LockfilePackage",
    "LockfileValidationResult",
    "ParameterSpec",
    "SignatureDiff",
    "SignatureSpec",
    "SymbolDefinitionVisitor",
    "SymbolImpactAnalyzer",
    "SymbolImpactReport",
    "TestImpactAnalyzer",
    "TestImpactReport",
    "analyze_diff_hunks",
    "analyze_python_code",
    "build_dependency_grounding_context",
    "filter_review_diff",
    "get_transitive_closure",
    "is_pure_dependency_pr",
    "parse_bumped_packages_from_diff",
    "parse_lockfile_dependency_graph",
    "render_deterministic_approval_markdown",
    "skip_reason",
    "validate_lockfile_diff",
]
