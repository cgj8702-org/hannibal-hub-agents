"""Heuristic and parity grounding checks for review text and metadata.

Provides extraction helpers for identifiers, symbols, and paths cited in review prose,
as well as parity and consistency checks against PR metadata and prior architectural decisions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from webhook_agent.review.grounding.citations import (
    _BACKTICK_RE,
    _IDENTIFIER_RE,
    _KNOWN_EXTENSIONS,
    _KNOWN_STDLIB_MODULE_PREFIXES,
    _PATH_RE,
    _STOPWORDS,
    _TOKEN_RE,
    GroundingIssue,
)

_TEST_CLAIM_RE = re.compile(r"(\d+)[^\d\n]{0,30}?tests?\b", re.I)
_ADDED_TEST_RE = re.compile(r"^\+\s*(?:async )?def test_", re.M)


def extract_identifiers(text: str) -> set[str]:
    """Extract lowercased identifier-like tokens from arbitrary text.

    Used on both sides of a provenance comparison -- the claim (what a review says it
    verified or resolved) and the artifact (the diff it was reviewing). Kept in one place
    so the fixture recorder and the offline harness cannot drift apart.
    """
    return {match.group(0).lower() for match in _IDENTIFIER_RE.finditer(text or "")}


def extract_cited_symbols(text: str) -> set[str]:
    """Extract the symbols a review explicitly cited in backticks.

    Backticks are the reviewer's own convention for naming code, so this is a
    high-precision read of "what did this review claim to be talking about".
    """
    symbols: set[str] = set()
    for match in _BACKTICK_RE.finditer(text or ""):
        for token in _IDENTIFIER_RE.findall(match.group(1)):
            symbols.add(token.lower())
    return symbols


def extract_cited_paths(text: str) -> set[str]:
    """Extract bare file paths a resolution names, lowercased and normalized.

    Catches "fixed in github_tools.py" or "see src/foo.py" with no backticks.
    Dotted calls like ``subprocess.run`` are symbols, not paths, and are
    excluded via the extension allowlist.
    """
    paths: set[str] = set()
    for match in _PATH_RE.finditer(text or ""):
        candidate = match.group(0).strip("./").lower()
        if any(candidate.startswith(pfx) for pfx in _KNOWN_STDLIB_MODULE_PREFIXES):
            continue
        if "/" in candidate:
            paths.add(candidate)
            continue
        ext = (match.group(1) or "").lower()
        if ext in _KNOWN_EXTENSIONS:
            paths.add(candidate)
    return paths


def extract_bare_symbols(text: str) -> set[str]:
    """Extract code-like identifiers named without backticks.

    A token counts only when it *looks like code*: snake_case or camelCase, a
    dunder, or dotted (``pr.get_files``). Plain English words ("fallback",
    "collection", "latency") are excluded so prose cannot manufacture a match --
    matching nothing keeps the evidence unverifiable (soft R2) rather than
    wrongly satisfying the rule.
    """
    text = text or ""
    backticked: set[str] = set()
    for match in _BACKTICK_RE.finditer(text):
        backticked.update(t.lower() for t in _IDENTIFIER_RE.findall(match.group(1)))
    bare = re.sub(_BACKTICK_RE, " ", text)
    bare = re.sub(_PATH_RE, " ", bare)
    symbols: set[str] = set()
    for token in _IDENTIFIER_RE.findall(bare):
        lowered = token.lower()
        if lowered in _STOPWORDS or lowered in backticked:
            continue
        if (
            "_" in token
            or token.startswith("__")
            or (any(c.islower() for c in token) and any(c.isupper() for c in token[1:]))
        ):
            symbols.add(lowered)
    return symbols


def check_test_count_claims(body: str, diff_text: str) -> list[GroundingIssue]:
    """Extract numeric test-count claims from review body and compare to diff recount.

    Extracts ``(\\d+)[^\\d\\n]{0,30}?tests?\\b`` from review body and compares each claimed
    number against a recount of added test functions (``^\\+\\s*(?:async )?def test_``).
    """
    issues: list[GroundingIssue] = []
    actual_added = sum(1 for _ in _ADDED_TEST_RE.finditer(diff_text or ""))
    for match in _TEST_CLAIM_RE.finditer(body or ""):
        claimed = int(match.group(1))
        if claimed != actual_added:
            issues.append(
                GroundingIssue(
                    severity="hard",
                    code="test_count_mismatch",
                    kind="suggestion",
                    path="",
                    line=None,
                    reason=(
                        f"Review claims {claimed} tests in '{match.group(0)}', but diff "
                        f"adds {actual_added} test functions."
                    ),
                )
            )
    return issues


def check_body_metadata_parity(body: str, metadata: Mapping[str, Any]) -> list[GroundingIssue]:
    """Verify parity between verified_invariants rendered in body vs footer metadata.

    verified_invariants are rendered in the body (e.g. under 'Verified Invariants')
    but omitted from serialization into the footer metadata (or metadata never reads it).
    """
    issues: list[GroundingIssue] = []
    body_has_invariants = "Verified Invariants" in (body or "")
    footer_invariants = metadata.get("verified_invariants")
    if body_has_invariants and not footer_invariants:
        issues.append(
            GroundingIssue(
                severity="hard",
                code="body_metadata_parity_mismatch",
                kind="invariant",
                path="",
                line=None,
                reason=(
                    "Review body renders 'Verified Invariants' section, but "
                    "verified_invariants is omitted from metadata footer."
                ),
            )
        )
    return issues


def detect_prior_decision_contradiction(
    prior_bodies: Sequence[str], current_text_or_diff: str
) -> bool:
    """Detect unacknowledged reversal of prior architectural decisions.

    For example, if a prior review (#241) approved removing `merge_pr`, and a later PR
    or review (#258) approves or restores `merge_pr`, this function detects the
    unacknowledged contradiction.
    """
    prior_removed_merge = any(
        "merge_pr" in b and ("remove" in b.lower() or "purge" in b.lower()) for b in prior_bodies
    )
    current_touches_merge = "merge_pr" in (current_text_or_diff or "")
    current_acknowledges = (
        "revers" in (current_text_or_diff or "").lower()
        or "withdraw" in (current_text_or_diff or "").lower()
    )

    return prior_removed_merge and current_touches_merge and not current_acknowledges


def ngram_containment(text_a: str, text_b: str, n: int = 5) -> float:
    """Calculate the n-gram containment ratio of text_b in text_a.

    Measures what fraction of sequence n-grams in text_b (e.g., review summary)
    appear in text_a (e.g., PR body). Returns float in [0.0, 1.0].
    Record only; never fail.
    """
    tokens_a = [t.lower() for t in _TOKEN_RE.findall(text_a or "")]
    tokens_b = [t.lower() for t in _TOKEN_RE.findall(text_b or "")]
    if len(tokens_b) < n:
        return 0.0
    ngrams_a = set(zip(*[tokens_a[i:] for i in range(n)], strict=False))
    ngrams_b = set(zip(*[tokens_b[i:] for i in range(n)], strict=False))
    if not ngrams_b:
        return 0.0
    intersection = ngrams_a & ngrams_b
    return round(len(intersection) / len(ngrams_b), 4)
