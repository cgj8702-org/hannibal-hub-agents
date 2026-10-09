"""Deterministic provenance checks for review citations.

This module answers exactly one question: *is a review's citation traceable to evidence
that exists?* It is deliberately pure -- no I/O, no client objects, no logging, no clock --
so both the offline replay harness and ``review()`` can use it.

It reuses the hunk-walking primitives in :mod:`webhook_agent.tools.diff_tools`
(``walk_right_side``) rather than maintaining a second diff parser. The dependency
direction follows the existing precedent in :mod:`webhook_agent.review.comment_poster`.

Severity model
--------------
``hard``
    A citation that cannot be true. Safe to reject.
``soft``
    A citation that cannot be *checked* with the evidence available, or that is merely
    suspicious. Do not reject on these until the false-positive rate has been measured;
    they exist so the pipeline can be instrumented before it is hardened.

Known limitation (read before trusting this module)
---------------------------------------------------
A wrong-but-existent citation -- right file, wrong line *inside* a changed hunk -- is
**not** detectable from ``path``/``line`` alone. The bot review of PR #258 cited
``tools/github_tools.py:556`` for a claim about the merge gate at lines 550-553; line
556 exists and is part of the diff, so every check below passes it. The durable fix is
to require the reviewer to quote the literal source line it is citing and compare that
quote against ``walk_right_side(diff)[1]`` (the right-side line text map). Until that
schema field exists, :func:`citation_overlap` is an advisory-only signal for the same
defect class.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from webhook_agent.tools.diff_tools import walk_right_side

Severity = Literal["hard", "soft"]
Kind = Literal["critical", "suggestion", "invariant"]

PLACEHOLDER_PATHS = frozenset({"codebase", "unknown", "n/a", "none", "null"})
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "are",
        "was",
        "were",
        "not",
        "any",
        "all",
        "its",
        "when",
        "then",
        "than",
        "which",
        "while",
        "will",
        "has",
        "have",
        "had",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "before",
        "after",
        "prior",
        "still",
        "only",
        "also",
        "each",
        "every",
        "verifies",
        "verified",
        "ensures",
        "ensure",
        "guarantees",
        "preserved",
        "invariant",
        "explicitly",
        "correctly",
        "properly",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A single review claim that carries a file/line citation."""

    kind: Kind
    path: str
    line: int | None
    evidence: str


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    """One failed or unverifiable citation check."""

    severity: Severity
    code: str
    kind: Kind
    path: str
    line: int | None
    reason: str

    @property
    def is_hard(self) -> bool:
        """Return True when this issue is safe to reject a review on."""
        return self.severity == "hard"


@dataclass(frozen=True, slots=True)
class CitationOverlap:
    """Advisory signal: how much the claim's wording overlaps the cited line's text."""

    path: str
    line: int | None
    overlap: float
    cited_text: str


def bare_path(path: str) -> str:
    """Drop git's ``a/``/``b/`` diff prefix from a path.

    Duplicates three lines of ``diff_tools._strip_diff_prefix`` on purpose: the original
    is private, and promoting it would be a drive-by refactor outside this change's scope.
    """
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def diff_text_from_patches(patches: Mapping[str, str | None]) -> str:
    """Synthesize unified-diff-shaped text from a ``{path: patch}`` mapping.

    PyGithub (and ``gh api .../pulls/<n>/files``) expose per-file patch bodies that begin
    at the ``@@`` hunk header, so a minimal ``--- a/`` / ``+++ b/`` preamble per file is
    enough for :func:`walk_right_side` to key line numbers by path.

    Files whose ``patch`` is ``None`` (binary, or too large for the API) are skipped; the
    caller should pass the authoritative changed-path list separately so those files are
    reported as *unverifiable* rather than as *not changed*.
    """
    parts: list[str] = []
    for path, patch in patches.items():
        if not patch:
            continue
        parts.append(f"--- a/{bare_path(path)}\n+++ b/{bare_path(path)}\n{patch}")
    return "\n".join(parts)


def verify_citations(
    findings: Sequence[Finding],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> list[GroundingIssue]:
    """Return one :class:`GroundingIssue` per citation that fails or cannot be checked.

    Args:
        findings: Review claims carrying a ``path``/``line`` citation.
        diff_text: The unified diff the review was performed against.
        changed_paths: Authoritative list of files the PR changed. Pass this whenever it
            is available (``pr.get_files()``): without it, a file that was changed but has
            no patch text is indistinguishable from a file the PR never touched. When
            ``None``, membership is inferred from the diff itself.

    Returns:
        Issues in input order. ``hard`` issues are safe to reject on; ``soft`` issues are
        for instrumentation until their false-positive rate is known.
    """
    anchors, line_text = walk_right_side(diff_text)
    if changed_paths is None:
        changed = set(line_text) | set(anchors)
    else:
        changed = {bare_path(p) for p in changed_paths if p}

    issues: list[GroundingIssue] = []
    for finding in findings:
        issues.extend(_check_one(finding, anchors, line_text, changed))
    return issues


def hard_reasons(issues: Sequence[GroundingIssue]) -> list[str]:
    """Return the model-facing reasons for the ``hard`` issues only."""
    return [issue.reason for issue in issues if issue.is_hard]


def citation_overlap(
    findings: Sequence[Finding],
    diff_text: str,
) -> list[CitationOverlap]:
    """Measure how much each claim's wording overlaps the text of its cited line.

    Advisory only -- never reject a review on this number. A low overlap is a *smell*:
    it means the reviewer is talking about something the line it pointed at does not
    appear to contain. Paraphrased or architectural claims legitimately score low, so
    use this to prioritise human inspection, not to gate.

    Returns:
        One entry per finding whose citation has line text available, in input order.
    """
    _, line_text = walk_right_side(diff_text)
    results: list[CitationOverlap] = []
    for finding in findings:
        path = bare_path((finding.path or "").strip())
        if finding.line is None:
            continue
        cited = (line_text.get(path) or {}).get(finding.line)
        if cited is None:
            continue
        claim_terms = _terms(finding.evidence)
        cited_terms = _terms(cited)
        overlap = len(claim_terms & cited_terms) / len(claim_terms) if claim_terms else 0.0
        results.append(
            CitationOverlap(
                path=path,
                line=finding.line,
                overlap=round(overlap, 4),
                cited_text=cited.strip(),
            )
        )
    return results


def _terms(text: str) -> set[str]:
    """Tokenize text into comparable lowercase terms, dropping stopwords."""
    return {
        token.lower() for token in _TOKEN_RE.findall(text or "") if token.lower() not in _STOPWORDS
    }


def _check_one(
    finding: Finding,
    anchors: dict[str, set[int]],
    line_text: dict[str, dict[int, str]],
    changed: set[str],
) -> list[GroundingIssue]:
    """Run every check applicable to a single finding."""
    issues: list[GroundingIssue] = []
    raw_path = (finding.path or "").strip()
    path = bare_path(raw_path)

    if not path or path.lower() in PLACEHOLDER_PATHS:
        issues.append(
            _issue(
                "hard",
                "empty_or_placeholder_path",
                finding,
                f"{finding.kind} has no exact file path (got {raw_path!r}); cite a real "
                f"path from the diff, or move the claim to unverified_claims.",
            )
        )
        return issues

    if path not in changed:
        issues.append(
            _issue(
                "hard",
                "path_not_changed",
                finding,
                f"{finding.kind} cites '{path}', which this PR does not change. If the "
                f"claim is about a pre-existing guard elsewhere, cite the changed file "
                f"that relies on it (or move it to unverified_claims).",
            )
        )
        return issues

    if path not in line_text and path not in anchors:
        issues.append(
            _issue(
                "soft",
                "path_unverifiable",
                finding,
                f"{finding.kind} cites '{path}', a changed file with no patch text "
                f"(binary or truncated). Cannot verify the line; treat as unverified.",
            )
        )

    if finding.line is None:
        issues.append(
            _issue(
                "hard",
                "missing_line",
                finding,
                f"{finding.kind} on '{path}' has no line number; cite the specific line.",
            )
        )
    elif finding.line <= 0:
        issues.append(
            _issue(
                "hard",
                "non_positive_line",
                finding,
                f"{finding.kind} on '{path}' has a non-positive line ({finding.line}).",
            )
        )
    else:
        known_lines = line_text.get(path)
        if known_lines is not None:
            if finding.line not in known_lines:
                issues.append(
                    _issue(
                        "soft",
                        "line_outside_diff",
                        finding,
                        f"{finding.kind} cites '{path}:{finding.line}', which the diff "
                        f"does not show. Cite a line the diff displays, or drop the line.",
                    )
                )
            elif finding.line not in anchors.get(path, set()):
                issues.append(
                    _issue(
                        "soft",
                        "context_only_line",
                        finding,
                        f"{finding.kind} cites '{path}:{finding.line}', which appears "
                        f"only as unchanged context, not as a line this PR adds or edits.",
                    )
                )

    if not (finding.evidence or "").strip():
        issues.append(
            _issue(
                "hard",
                "missing_evidence",
                finding,
                f"{finding.kind} on '{path}' has no evidence; state what in the diff "
                f"proves the claim.",
            )
        )

    return issues


def _issue(
    severity: Severity,
    code: str,
    finding: Finding,
    reason: str,
) -> GroundingIssue:
    """Build a GroundingIssue carrying the original citation."""
    return GroundingIssue(
        severity=severity,
        code=code,
        kind=finding.kind,
        path=finding.path,
        line=finding.line,
        reason=reason,
    )


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Bare file paths with a directory or extension: src/foo.py, docs/plan.md,
# tests/unit/review/test_x.py. Guarded to need a slash or a dot so prose like
# "the fallback path" does not match.
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,5})"
    r"(?![A-Za-z0-9_.-])"
)


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
    """
    paths: set[str] = set()
    for match in _PATH_RE.finditer(text or ""):
        candidate = match.group(0).strip("./").lower()
        if "/" in candidate or "." in candidate:
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
    symbols: set[str] = set()
    for token in _IDENTIFIER_RE.findall(bare):
        lowered = token.lower()
        if lowered in _STOPWORDS or lowered in backticked:
            continue
        if (
            "_" in token
            or token.startswith("__")
            or (token[0].islower() and any(c.isupper() for c in token[1:]))
        ):
            symbols.add(lowered)
    return symbols


__all__ = [
    "PLACEHOLDER_PATHS",
    "CitationOverlap",
    "Finding",
    "GroundingIssue",
    "Kind",
    "Severity",
    "bare_path",
    "citation_overlap",
    "diff_text_from_patches",
    "extract_bare_symbols",
    "extract_cited_paths",
    "extract_cited_symbols",
    "extract_identifiers",
    "hard_reasons",
    "verify_citations",
]
