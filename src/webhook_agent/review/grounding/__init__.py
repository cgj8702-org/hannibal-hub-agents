"""Deterministic provenance and grounding checks for review citations.

Modular package partitioning:
- citations: Line-level citation verification, overlap calculations, finding extraction.
- resolutions: Prior-review comment resolution verification and hunk parsing.
- invariants: Mechanical verification, semantic invariant fact-checking, scorecard invariant recovery.
- heuristics: Identifier/symbol/path extraction, test count claims, metadata parity, n-gram containment.
"""

from __future__ import annotations

from webhook_agent.constants import STRICT_REVIEW_GROUNDING
from webhook_agent.review.grounding.citations import (
    PLACEHOLDER_PATHS,
    CitationOverlap,
    Finding,
    GroundingIssue,
    Kind,
    Severity,
    bare_path,
    citation_overlap,
    diff_text_from_patches,
    extract_grounding_findings,
    hard_reasons,
    strict_grounding_enabled,
    touched_lines_by_file,
    verify_citations,
)
from webhook_agent.review.grounding.heuristics import (
    check_body_metadata_parity,
    check_test_count_claims,
    detect_prior_decision_contradiction,
    extract_bare_symbols,
    extract_cited_paths,
    extract_cited_symbols,
    extract_identifiers,
    ngram_containment,
)
from webhook_agent.review.grounding.invariants import (
    BEHAVIORAL_KEYWORDS,
    enforce_invariant_dominance,
    extract_scorecard_invariants,
    filter_and_ground_semantic_invariants,
    is_machine_verified_invariant,
    partition_invariants,
    promote_semantic_to_verified,
    verify_semantic_invariant,
)
from webhook_agent.review.grounding.resolutions import (
    parse_removal_hunks,
    verify_resolutions,
)

__all__ = [
    "BEHAVIORAL_KEYWORDS",
    "PLACEHOLDER_PATHS",
    "STRICT_REVIEW_GROUNDING",
    "CitationOverlap",
    "Finding",
    "GroundingIssue",
    "Kind",
    "Severity",
    "bare_path",
    "check_body_metadata_parity",
    "check_test_count_claims",
    "citation_overlap",
    "detect_prior_decision_contradiction",
    "diff_text_from_patches",
    "enforce_invariant_dominance",
    "extract_bare_symbols",
    "extract_cited_paths",
    "extract_cited_symbols",
    "extract_grounding_findings",
    "extract_identifiers",
    "extract_scorecard_invariants",
    "filter_and_ground_semantic_invariants",
    "hard_reasons",
    "is_machine_verified_invariant",
    "ngram_containment",
    "parse_removal_hunks",
    "partition_invariants",
    "promote_semantic_to_verified",
    "strict_grounding_enabled",
    "touched_lines_by_file",
    "verify_citations",
    "verify_resolutions",
    "verify_semantic_invariant",
]
