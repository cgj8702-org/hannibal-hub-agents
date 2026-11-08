"""Static code analysis, Python AST parsing, symbol graphs, and test impact evaluation."""

from __future__ import annotations

from .architecture_scanner import (
    ArchitecturalBoundaryScanner,
    ArchitecturalFinding,
    ArchitecturalReport,
    evaluate_architecture_contract,
)
from .concurrency_scanner import (
    ConcurrencyAnalysisReport,
    ConcurrencyFinding,
    ConcurrencyScanner,
    evaluate_concurrency_integrity,
)
from .dependency_tree import (
    LockfilePackage,
    build_dependency_grounding_context,
    get_transitive_closure,
    parse_lockfile_dependency_graph,
)
from .diff_filter import (
    filter_review_diff,
    is_reviewable_path,
    reviewable_file_paths,
    skip_reason,
)
from .lockfile_validator import (
    LockfileValidationResult,
    is_pure_dependency_pr,
    parse_bumped_packages_from_diff,
    render_deterministic_approval_markdown,
    validate_lockfile_diff,
)
from .loop_scanner import (
    LoopTerminationFinding,
    LoopTerminationReport,
    LoopTerminationScanner,
    evaluate_loop_termination,
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
from .tautology_scanner import (
    TautologyFinding,
    TautologyReport,
    TautologyScanner,
    evaluate_tautology_integrity,
)
from .test_impact import (
    TestImpactAnalyzer,
    TestImpactReport,
    evaluate_test_coverage_integrity,
    run_diff_coverage,
)

__all__ = [
    "ArchitecturalBoundaryScanner",
    "ArchitecturalFinding",
    "ArchitecturalReport",
    "BrokenCallSite",
    "CallSiteSpec",
    "CallSiteVisitor",
    "ConcurrencyAnalysisReport",
    "ConcurrencyFinding",
    "ConcurrencyScanner",
    "ConfidenceLevel",
    "LockfilePackage",
    "LockfileValidationResult",
    "LoopTerminationFinding",
    "LoopTerminationReport",
    "LoopTerminationScanner",
    "ParameterSpec",
    "SecurityAnalysisReport",
    "SecurityFinding",
    "SecurityScanner",
    "SignatureDiff",
    "SignatureSpec",
    "SymbolDefinitionVisitor",
    "SymbolImpactAnalyzer",
    "SymbolImpactReport",
    "TautologyFinding",
    "TautologyReport",
    "TautologyScanner",
    "TestImpactAnalyzer",
    "TestImpactReport",
    "build_dependency_grounding_context",
    "evaluate_architecture_contract",
    "evaluate_concurrency_integrity",
    "evaluate_contract_integrity",
    "evaluate_loop_termination",
    "evaluate_security_integrity",
    "evaluate_tautology_integrity",
    "evaluate_test_coverage_integrity",
    "filter_review_diff",
    "get_transitive_closure",
    "is_exempt_file_path",
    "is_exempt_secret_value",
    "is_pure_dependency_pr",
    "is_reviewable_path",
    "parse_bumped_packages_from_diff",
    "parse_lockfile_dependency_graph",
    "render_deterministic_approval_markdown",
    "reviewable_file_paths",
    "run_diff_coverage",
    "skip_reason",
    "validate_lockfile_diff",
]
