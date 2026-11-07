"""Deterministic provenance checks for review citations.

This module answers exactly one question: *is a review's citation traceable to evidence
that exists?* It is deliberately pure -- no I/O, no client objects, no clock -- so both
unit tests and ``review()`` can use it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from webhook_agent.constants import STRICT_REVIEW_GROUNDING
from webhook_agent.tools.diff_tools import walk_right_side

logger = logging.getLogger("webhook_agent.review.grounding.citations")

Severity = Literal["hard", "soft"]
Kind = Literal["critical", "suggestion", "invariant", "scorecard"]

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

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_KNOWN_EXTENSIONS = frozenset(
    {
        "py",
        "md",
        "txt",
        "rst",
        "json",
        "yaml",
        "yml",
        "toml",
        "sh",
        "bash",
        "js",
        "ts",
        "tsx",
        "jsx",
        "go",
        "rs",
        "java",
        "rb",
        "c",
        "h",
        "cpp",
        "hpp",
        "css",
        "html",
        "xml",
        "sql",
        "lock",
        "diff",
        "patch",
    }
)
_KNOWN_STDLIB_MODULE_PREFIXES = frozenset(
    {
        "asyncio.",
        "threading.",
        "multiprocessing.",
        "concurrent.",
        "subprocess.",
        "logging.",
        "os.",
        "sys.",
        "json.",
        "re.",
        "math.",
        "typing.",
        "pathlib.",
    }
)
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"|[A-Za-z0-9_.-]+\.([A-Za-z0-9]{1,5})"
    r"(?![A-Za-z0-9_.-])"
)
_INVARIANT_CITATION_RE = re.compile(
    r"(?:^|[\s(])`?([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9_\-]+):(\d+)(?:[-,]\d+)*`?"
)

BEHAVIORAL_KEYWORDS: tuple[str, ...] = (
    "skips",
    "skip",
    "handles",
    "fails",
    "retries",
    "bypasses",
    "fallback",
    "runs",
    "gracefully",
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
    """Drop git's ``a/``/``b/`` diff prefix from a path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _norm_evidence(text: str) -> str:
    """Normalize text by stripping backticks, collapsing whitespace, and lowercasing."""
    cleaned = (text or "").replace("`", "")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def touched_lines_by_file(patches: Mapping[str, str | None]) -> dict[str, set[int]]:
    """Map each file path to its set of modified or added new-side line numbers."""
    from webhook_agent.tools.diff_tools import added_line_anchors

    result: dict[str, set[int]] = {}
    for path, patch in patches.items():
        bp = bare_path(path)
        if not patch:
            result[bp] = set()
        else:
            diff_text = diff_text_from_patches({path: patch})
            anchors = added_line_anchors(diff_text)
            result[bp] = anchors.get(bp, set())
    return result


def diff_text_from_patches(patches: Mapping[str, str | None]) -> str:
    """Synthesize unified-diff-shaped text from a ``{path: patch}`` mapping."""
    parts: list[str] = []
    for path, patch in patches.items():
        if not patch:
            continue
        parts.append(f"--- a/{bare_path(path)}\n+++ b/{bare_path(path)}\n{patch}")
    return "\n".join(parts)


def _is_deletion_refactor_pr(diff_text: str) -> bool:
    """Detect if a PR is a pure deletion/refactor with few added lines in code files."""
    if not diff_text:
        return False

    code_extensions = {
        f".{ext}"
        for ext in (
            "py",
            "js",
            "ts",
            "tsx",
            "jsx",
            "go",
            "rs",
            "java",
            "rb",
            "c",
            "h",
            "cpp",
            "hpp",
            "cs",
            "php",
            "swift",
            "kt",
            "scala",
            "clj",
            "hs",
            "ml",
            "fs",
            "vb",
            "pl",
            "pm",
            "t",
            "sh",
            "bash",
            "zsh",
            "fish",
            "ps1",
            "bat",
            "cmd",
            "sql",
            "r",
            "m",
            "jl",
            "lua",
            "rkt",
            "ss",
            "scm",
            "cl",
            "lsp",
            "el",
            "vim",
            "ex",
            "exs",
            "erl",
            "hrl",
            "dart",
        )
    }

    added_lines_count = 0
    removed_lines_count = 0
    files_changed = set()
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith(("--- a/", "+++ b/")):
            current_file = line[6:].strip()
            files_changed.add(current_file)
        elif line.startswith("+") and not line.startswith("+++"):
            if any(current_file.endswith(ext) for ext in code_extensions):
                added_lines_count += 1
        elif line.startswith("-") and not line.startswith("---"):
            if any(current_file.endswith(ext) for ext in code_extensions):
                removed_lines_count += 1

    if added_lines_count == 0 and removed_lines_count == 0:
        return False

    total_changed = added_lines_count + removed_lines_count
    if total_changed == 0:
        return False

    if added_lines_count >= 50:
        return False

    if removed_lines_count > added_lines_count * 3:
        return True

    return bool(
        total_changed < 10
        and abs(added_lines_count - removed_lines_count) <= 2
        and len(files_changed) > 1
    )


def verify_citations(
    findings: Sequence[Finding],
    touched_or_diff: dict[str, set[int]] | str,
    diff_text: str | None = None,
    changed_paths: Collection[str] | None = None,
) -> list[GroundingIssue]:
    """Return one :class:`GroundingIssue` per citation that fails or cannot be checked."""
    if isinstance(touched_or_diff, dict):
        anchors = touched_or_diff
        actual_diff_text = diff_text or ""
        _, line_text = walk_right_side(actual_diff_text)
        if changed_paths is None:
            changed = set(anchors.keys())
        else:
            changed = {bare_path(p) for p in changed_paths if p}
    else:
        actual_diff_text = touched_or_diff
        if changed_paths is None and not isinstance(diff_text, str):
            changed_paths = diff_text
            actual_diff_text_for_walk = actual_diff_text
            actual_diff_text = ""
        else:
            actual_diff_text_for_walk = actual_diff_text

        anchors, line_text = walk_right_side(actual_diff_text_for_walk)
        if changed_paths is None:
            changed = set(line_text) | set(anchors)
        else:
            changed = {bare_path(p) for p in changed_paths if p}
        if not actual_diff_text:
            actual_diff_text = actual_diff_text_for_walk

    is_deletion_refactor = _is_deletion_refactor_pr(actual_diff_text)

    issues: list[GroundingIssue] = []
    for finding in findings:
        issues.extend(
            _check_one(finding, anchors, line_text, changed, actual_diff_text, is_deletion_refactor)
        )
    return issues


def hard_reasons(issues: Sequence[GroundingIssue]) -> list[str]:
    """Return the model-facing reasons for the ``hard`` issues only."""
    return [issue.reason for issue in issues if issue.is_hard]


def citation_overlap(
    findings: Sequence[Finding],
    diff_text: str,
) -> list[CitationOverlap]:
    """Measure how much each claim's wording overlaps the text of its cited line."""
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
    diff_text: str = "",
    is_deletion_refactor: bool = False,
) -> list[GroundingIssue]:
    """Run every check applicable to a single finding."""
    issues: list[GroundingIssue] = []
    raw_path = (finding.path or "").strip()
    path = bare_path(raw_path)

    if not path or path.lower() in PLACEHOLDER_PATHS:
        issues.append(
            _issue(
                "soft" if finding.kind == "suggestion" else "hard",
                "empty_or_placeholder_path",
                finding,
                f"{finding.kind} has no exact file path (got {raw_path!r}); cite a real "
                f"path from the diff, or move the claim to unverified_claims.",
            )
        )
        return issues

    if path not in changed:
        if finding.kind == "scorecard":
            issues.append(
                _issue(
                    "soft",
                    "scorecard_path_not_changed",
                    finding,
                    f"Scorecard note cites '{path}', which is not changed by this PR.",
                )
            )
            return issues
        if is_deletion_refactor:
            issues.append(
                _issue(
                    "soft",
                    "path_not_changed",
                    finding,
                    f"{finding.kind} cites '{path}', which this PR does not change. If the "
                    f"claim is about a pre-existing guard elsewhere, cite the changed file "
                    f"that relies on it (or move it to unverified_claims).",
                )
            )
            return issues
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
        if finding.kind == "suggestion":
            issues.append(
                _issue(
                    "soft",
                    "file_level_suggestion",
                    finding,
                    f"Suggestion on '{path}' is file-level (no line number).",
                )
            )
        else:
            issues.append(
                _issue(
                    "hard",
                    "missing_line",
                    finding,
                    f"{finding.kind} on '{path}' has no line number; cite the specific line.",
                )
            )
    elif finding.line <= 0:
        if finding.kind == "suggestion":
            issues.append(
                _issue(
                    "soft",
                    "non_positive_line",
                    finding,
                    f"Suggestion on '{path}' has a non-positive line ({finding.line}).",
                )
            )
        else:
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
                "soft" if (is_deletion_refactor or finding.kind == "suggestion") else "hard",
                "missing_evidence",
                finding,
                f"{finding.kind} on '{path}' has no evidence; state what in the diff "
                f"proves the claim.",
            )
        )
    else:
        raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(finding.evidence or "") if q.strip()]
        code_quotes = [q for q in raw_quotes if not _is_path_citation(q, path)]
        if diff_text and finding.kind not in ("scorecard", "suggestion"):
            norm_diff = _norm_evidence(diff_text)
            for quote in code_quotes:
                if len(_norm_evidence(quote)) > 24:
                    norm_code = _norm_evidence(quote)
                    if norm_code not in norm_diff:
                        issues.append(
                            _issue(
                                "soft" if is_deletion_refactor else "hard",
                                "evidence_not_in_diff",
                                finding,
                                f"{finding.kind} on '{path}:{finding.line}' evidence text "
                                f"'{quote}' does not appear in the diff.",
                            )
                        )

    if finding.kind == "invariant":
        ev = (finding.evidence or "").lower()
        is_behavioral = any(kw in ev for kw in BEHAVIORAL_KEYWORDS)
        raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(finding.evidence or "") if q.strip()]
        code_quotes = [q for q in raw_quotes if not _is_path_citation(q, path)]
        norm_diff = _norm_evidence(diff_text)
        has_diff_quote = bool(code_quotes) and all(
            _norm_evidence(q) in norm_diff for q in code_quotes
        )
        has_test = re.search(r"\btest_\w+", ev) is not None
        has_concrete_quote_or_test = has_diff_quote or has_test
        is_static_fact = (
            re.search(r"^[A-Z0-9_]+\s*=\s*", (finding.evidence or "").strip()) is not None
        )
        if is_behavioral and not is_static_fact and not has_concrete_quote_or_test:
            issues.append(
                _issue(
                    "soft" if is_deletion_refactor else "hard",
                    "unsubstantiated_behavioral_invariant",
                    finding,
                    f"Invariant claim on '{path}' asserts behavioral logic ('{finding.evidence}') "
                    f"without a concrete code quote or test function citation.",
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


def _is_path_citation(candidate: str, finding_path: str = "") -> bool:
    """Return True if a backticked candidate string represents a file/line citation rather than code."""
    c = candidate.strip()
    if not c or c.startswith(("self.", "cls.")):
        return False
    if _INVARIANT_CITATION_RE.search(c):
        return True
    if " " in c or "(" in c or ")" in c or "=" in c or "\n" in c:
        return False
    if finding_path and (
        c == finding_path or finding_path.endswith("/" + c) or c.endswith("/" + finding_path)
    ):
        return True
    if "/" in c:
        parts = c.split("/")
        last_ext = (parts[-1].rsplit(".", 1)[-1] if "." in parts[-1] else "").lower()
        if last_ext in _KNOWN_EXTENSIONS:
            return True
    parts = c.rsplit(".", 1)
    return len(parts) == 2 and parts[1].lower() in _KNOWN_EXTENSIONS


def _citation_finding(kind: Kind, item: Any) -> Finding:
    """Adapt one parsed review finding item into a grounding citation Finding."""
    evidence = getattr(item, "evidence", None) or getattr(item, "description", None) or ""
    if isinstance(item, dict):
        evidence = item.get("evidence") or item.get("description") or item.get("invariant") or ""
        path = item.get("path") or ""
        line = item.get("line")
    else:
        path = getattr(item, "path", None) or ""
        line = getattr(item, "line", None)
    return Finding(
        kind=kind,
        path=path,
        line=line,
        evidence=str(evidence),
    )


def extract_grounding_findings(review_obj: Any) -> list[Finding]:
    """Adapt cited findings from CodeReviewResponse or SyncReviewResponse into Findings."""
    findings: list[Finding] = []
    if review_obj is None:
        return findings

    critical_items = getattr(review_obj, "critical_issues", None)
    if critical_items is None and isinstance(review_obj, dict):
        critical_items = review_obj.get("critical_issues")
    for item in critical_items or []:
        findings.append(_citation_finding("critical", item))

    suggestion_items = getattr(review_obj, "minor_suggestions", None)
    if suggestion_items is None and isinstance(review_obj, dict):
        suggestion_items = review_obj.get("minor_suggestions")
    for item in suggestion_items or []:
        findings.append(_citation_finding("suggestion", item))

    invariant_items = getattr(review_obj, "verified_invariants", None)
    if invariant_items is None and isinstance(review_obj, dict):
        invariant_items = review_obj.get("verified_invariants")
    for item in invariant_items or []:
        if isinstance(item, str):
            match = _INVARIANT_CITATION_RE.search(item)
            if match:
                findings.append(
                    Finding(
                        kind="invariant",
                        path=match.group(1),
                        line=int(match.group(2)),
                        evidence=item,
                    )
                )
        elif hasattr(item, "path") or isinstance(item, dict):
            findings.append(_citation_finding("invariant", item))

    scorecard_items = getattr(review_obj, "scorecard", None)
    if scorecard_items is None and isinstance(review_obj, dict):
        scorecard_items = review_obj.get("scorecard")
    for item in scorecard_items or []:
        notes = (
            getattr(item, "notes", None) or (item.get("notes") if isinstance(item, dict) else "")
        ) or ""
        for m in _INVARIANT_CITATION_RE.finditer(notes):
            try:
                line_val = int(m.group(2))
            except (ValueError, TypeError):
                line_val = None
            findings.append(
                Finding(
                    kind="scorecard",
                    path=bare_path(m.group(1)),
                    line=line_val,
                    evidence=notes,
                )
            )

    return findings


def strict_grounding_enabled() -> bool:
    """Return True when ungrounded citations should reject or downgrade a review, not merely log."""
    with contextlib.suppress(Exception):
        import webhook_agent.review.grounding as grounding_pkg

        default_val = getattr(grounding_pkg, "STRICT_REVIEW_GROUNDING", STRICT_REVIEW_GROUNDING)
        val = os.environ.get("STRICT_REVIEW_GROUNDING", default_val)
        return str(val).strip().lower() in ("1", "true", "yes")

    val = os.environ.get("STRICT_REVIEW_GROUNDING", STRICT_REVIEW_GROUNDING)
    return str(val).strip().lower() in ("1", "true", "yes")
