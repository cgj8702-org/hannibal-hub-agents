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
from .security_scanner import (
    SecurityAnalysisReport,
    SecurityFinding,
    SecurityScanner,
    evaluate_security_integrity,
    is_exempt_file_path,
    is_exempt_secret_value,
)
from .symbol_graph import (
    BrokenCallSite,
    CallSiteSpec,
    CallSiteVisitor,
    ConfidenceLevel,
    ParameterSpec,
    SignatureDiff,
    SignatureSpec,
    SymbolDefinitionVisitor,
    SymbolImpactAnalyzer,
    SymbolImpactReport,
    evaluate_contract_integrity,
)
from .test_impact import TestImpactAnalyzer, TestImpactReport

__all__ = [
    "ASTAnalysisResult",
    "BrokenCallSite",
    "CallSiteSpec",
    "CallSiteVisitor",
    "ConfidenceLevel",
    "LockfilePackage",
    "LockfileValidationResult",
    "ParameterSpec",
    "SecurityAnalysisReport",
    "SecurityFinding",
    "SecurityScanner",
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
    "evaluate_contract_integrity",
    "evaluate_security_integrity",
    "filter_review_diff",
    "get_transitive_closure",
    "is_exempt_file_path",
    "is_exempt_secret_value",
    "is_pure_dependency_pr",
    "parse_bumped_packages_from_diff",
    "parse_lockfile_dependency_graph",
    "render_deterministic_approval_markdown",
    "skip_reason",
    "validate_lockfile_diff",
]
