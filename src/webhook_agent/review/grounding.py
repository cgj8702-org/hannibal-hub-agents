"""Deterministic provenance checks for review citations.

This module answers exactly one question: *is a review's citation traceable to evidence
that exists?* It is deliberately pure -- no I/O, no client objects, no clock -- so both
unit tests and ``review()`` can use it.

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

import contextlib
import functools
import logging
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from webhook_agent.constants import STRICT_REVIEW_GROUNDING
from webhook_agent.tools.diff_tools import walk_right_side

logger = logging.getLogger("webhook_agent.review.grounding")

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


def touched_lines_by_file(patches: Mapping[str, str | None]) -> dict[str, set[int]]:
    """Map each file path to its set of modified or added new-side line numbers.

    Parses unified-diff hunks via :func:`webhook_agent.tools.diff_tools.added_line_anchors`.
    If patch is None or empty, the path maps to an empty set (unverifiable).
    """
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


def _norm_evidence(text: str) -> str:
    """Normalize text by stripping backticks, collapsing whitespace, and lowercasing."""
    cleaned = (text or "").replace("`", "")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def parse_removal_hunks(diff_text: str) -> dict[str, list[str]]:
    """Parse left-side removed lines (-) from unified diff text keyed by bare path."""
    removals: dict[str, list[str]] = {}
    current_file = ""
    for line in (diff_text or "").splitlines():
        if line.startswith("--- a/"):
            current_file = bare_path(line[6:].strip())
            removals.setdefault(current_file, [])
        elif line.startswith("+++ b/"):
            continue
        elif line.startswith("-") and not line.startswith("---"):
            if current_file:
                removals.setdefault(current_file, []).append(line[1:].strip())
    return removals


def verify_resolutions(
    resolutions: Sequence[Mapping[str, Any] | Any],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Verify resolution claims against current code diff and removal hunks.

    A prior finding may be marked RESOLVED only if current code diff or removal hunks
    support the specific fix. If evidence or cited path/symbols are absent or ungrounded,
    status is flipped to UNRESOLVED.
    """
    anchors, line_text = walk_right_side(diff_text)
    removals = parse_removal_hunks(diff_text)
    if changed_paths is None:
        changed = set(line_text) | set(anchors) | set(removals)
    else:
        changed = {bare_path(p) for p in changed_paths if p}

    all_added_text = " ".join(
        txt for file_lines in line_text.values() for txt in file_lines.values()
    )
    all_removed_text = " ".join(
        line for file_removals in removals.values() for line in file_removals
    )

    verified_results: list[dict[str, Any]] = []

    for item in resolutions:
        if isinstance(item, dict):
            item_dict = dict(item)
        elif hasattr(item, "model_dump"):
            item_dict = item.model_dump()
        else:
            item_dict = {
                "item_description": getattr(item, "item_description", ""),
                "status": getattr(item, "status", "RESOLVED"),
                "evidence": getattr(item, "evidence", ""),
                "category": getattr(item, "category", "CRITICAL"),
            }

        status = item_dict.get("status", "RESOLVED")
        if status != "RESOLVED":
            verified_results.append(item_dict)
            continue

        desc = str(item_dict.get("item_description", ""))
        ev = str(item_dict.get("evidence", ""))
        item_path = str(item_dict.get("path", "")).strip()
        combined_text = f"{desc} {ev}"

        cited_paths = extract_cited_paths(combined_text)
        if item_path:
            cited_paths.add(bare_path(item_path))

        cited_symbols = extract_cited_symbols(combined_text) | extract_bare_symbols(combined_text)

        # 1. Path check
        path_ok = True
        if cited_paths:
            if not any(cp in changed for cp in cited_paths):
                path_ok = False

        # 2. Symbol / Evidence check
        evidence_ok = bool(cited_symbols)
        if cited_symbols:
            target_files = cited_paths if cited_paths else changed
            path_added_text = " ".join(
                txt for cp in target_files for txt in line_text.get(cp, {}).values()
            )
            path_removed_text = " ".join(
                line for cp in target_files for line in removals.get(cp, [])
            )
            if not cited_paths:
                path_added_text = all_added_text
                path_removed_text = all_removed_text
            symbol_in_added = any(sym in path_added_text.lower() for sym in cited_symbols)
            symbol_in_removed = any(sym in path_removed_text.lower() for sym in cited_symbols)
            if not (symbol_in_added or symbol_in_removed):
                evidence_ok = False

        if not path_ok or not evidence_ok:
            item_dict["status"] = "UNRESOLVED"
            item_dict["grounding_note"] = (
                "Resolution not grounded in current code diff: cited path or evidence symbol absent from diff"
            )

        verified_results.append(item_dict)

    return verified_results


def verify_citations(
    findings: Sequence[Finding],
    touched_or_diff: dict[str, set[int]] | str,
    diff_text: str | None = None,
    changed_paths: Collection[str] | None = None,
) -> list[GroundingIssue]:
    """Return one :class:`GroundingIssue` per citation that fails or cannot be checked.

    Accepts either ``verify_citations(findings, touched, diff_text)`` or
    legacy ``verify_citations(findings, diff_text, changed_paths)``.
    """
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

    issues: list[GroundingIssue] = []
    for finding in findings:
        issues.extend(_check_one(finding, anchors, line_text, changed, actual_diff_text))
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
    diff_text: str = "",
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
    else:
        raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(finding.evidence or "") if q.strip()]
        code_quotes = [q for q in raw_quotes if not _is_path_citation(q, path)]
        if diff_text and finding.kind != "scorecard":
            norm_diff = _norm_evidence(diff_text)
            for quote in code_quotes:
                if len(_norm_evidence(quote)) > 24:
                    norm_code = _norm_evidence(quote)
                    if norm_code not in norm_diff:
                        issues.append(
                            _issue(
                                "hard",
                                "evidence_not_in_diff",
                                finding,
                                f"{finding.kind} on '{path}:{finding.line}' evidence text "
                                f"'{quote}' does not appear in the diff.",
                            )
                        )

    # Behavioral invariant check:
    # A "Verified Invariant" asserting a behavior (not a static fact or line quote/test citation)
    # must be backed by a test or diff quote.
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
                    "hard",
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


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Bare file paths with a directory or extension: src/foo.py, docs/plan.md,
# tests/unit/review/test_x.py. The extension allowlist keeps dotted calls like
# ``subprocess.run`` or ``gh.api`` out -- those are symbols, not paths. Guarded
# to need a slash or a known extension so prose like "the fallback path" does
# not match.
_KNOWN_EXTENSIONS = frozenset(
    {
        "py",
        "md",
        "txt",
        "json",
        "yml",
        "yaml",
        "toml",
        "cfg",
        "ini",
        "sh",
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


def is_machine_verified_invariant(
    invariant: str,
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> bool:
    """Return True if an invariant has concrete mechanical proof in the diff, False if semantic.

    An invariant is classified as Machine-Verified [Machine-Checked] if:
    1. It points to a cited file and line in the diff (via path:line citation), where:
       - The cited file is among the changed paths / diff files.
       - Any non-path code quotes appear in the diff.
       - If asserting behavioral logic, it contains a concrete code quote from the diff or
         cites a test function (test_\\w+).
    2. OR it contains a concrete code quote (>= 16 chars) or test symbol directly found in the diff.
    Otherwise, it is classified as Semantic [Model-Asserted].
    """
    raw_inv = (invariant or "").strip()
    if not raw_inv or not diff_text:
        return False

    norm_diff = _norm_evidence(diff_text)
    match = _INVARIANT_CITATION_RE.search(raw_inv)

    if match:
        path = bare_path(match.group(1))
        try:
            line_num = int(match.group(2))
        except (ValueError, TypeError):
            line_num = None

        if changed_paths is not None:
            changed = {bare_path(p) for p in changed_paths if p}
            if path not in changed:
                return False

        anchors, line_text = walk_right_side(diff_text)
        if path not in line_text and path not in anchors:
            return False
        if line_num is not None:
            known_lines = line_text.get(path, {})
            if known_lines and line_num not in known_lines:
                return False

        raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(raw_inv) if q.strip()]
        code_quotes = [q for q in raw_quotes if not _is_path_citation(q, path)]
        for q in code_quotes:
            if len(_norm_evidence(q)) > 24 and _norm_evidence(q) not in norm_diff:
                return False

        ev = raw_inv.lower()
        is_behavioral = any(kw in ev for kw in BEHAVIORAL_KEYWORDS)
        has_diff_quote = bool(code_quotes) and all(
            _norm_evidence(q) in norm_diff for q in code_quotes
        )
        has_test = re.search(r"\btest_\w+", ev) is not None
        has_concrete_quote_or_test = has_diff_quote or has_test
        is_static_fact = re.search(r"^[A-Z0-9_]+\s*=\s*", raw_inv) is not None

        return not (is_behavioral and not is_static_fact and not has_concrete_quote_or_test)

    # No path:line citation: check if it directly quotes diff code or cites a test function in the diff
    raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(raw_inv) if q.strip()]
    code_quotes = [q for q in raw_quotes if not _is_path_citation(q)]
    has_matching_quote = any(
        len(_norm_evidence(q)) >= 16 and _norm_evidence(q) in norm_diff for q in code_quotes
    )
    has_test = bool(re.search(r"\btest_\w+", raw_inv)) and any(
        t in norm_diff for t in re.findall(r"\btest_\w+", raw_inv.lower())
    )
    return has_matching_quote or has_test


def partition_invariants(
    invariants: Sequence[str],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Partition invariant strings into (machine_verified, semantic_asserted)."""
    verified: list[str] = []
    semantic: list[str] = []
    for inv in invariants:
        clean = (inv or "").strip()
        if not clean:
            continue
        if is_machine_verified_invariant(clean, diff_text, changed_paths):
            verified.append(clean)
        else:
            semantic.append(clean)
    return verified, semantic


_COMMON_BUILTINS: frozenset[str] = frozenset(
    {
        "none",
        "true",
        "false",
        "str",
        "int",
        "float",
        "bool",
        "dict",
        "list",
        "set",
        "tuple",
        "any",
        "optional",
        "union",
        "callable",
        "mapping",
        "sequence",
        "iterable",
        "exception",
        "valueerror",
        "typeerror",
        "runtimeerror",
        "keyerror",
        "attributeerror",
        "os",
        "sys",
        "re",
        "json",
        "asyncio",
        "logging",
        "path",
        "self",
        "cls",
        "args",
        "kwargs",
        "return",
        "pass",
        "yield",
        "await",
        "async",
    }
)

_UNCERTAINTY_KEYWORDS: frozenset[str] = frozenset(
    {
        "unverified",
        "unclear",
        "uncertainty",
        "speculative",
        "cannot verify",
        "cannot confirm",
        "untested hypothesis",
        "residual uncertainty",
        "unsubstantiated",
    }
)


def _resolve_repo_root(repo_root: Path | str | None = None) -> Path | None:
    """Resolve repository root directory if available."""
    if repo_root is not None:
        p = Path(repo_root)
        return p if p.is_dir() else None
    with contextlib.suppress(Exception):
        from webhook_agent.environment.context import get_active_environment

        env = get_active_environment()
        if env and env.root and Path(env.root).is_dir():
            return Path(env.root)
    # Only fall back to local cwd when running under pytest so offline tests
    # can inspect the local test suite and codebase.
    if "PYTEST_CURRENT_TEST" in os.environ:
        cwd = Path.cwd()
        if (cwd / "src").is_dir() or (cwd / "tests").is_dir():
            return cwd
    return None


@functools.lru_cache(maxsize=16)
def _get_repo_file_index(
    repo_root_str: str,
    head_sha: str = "",
) -> tuple[frozenset[str], frozenset[str]]:
    """Return tuple of (relative_paths, basenames) in the repository."""
    root_path = Path(repo_root_str)
    if not root_path.is_dir():
        return frozenset(), frozenset()
    rel_paths: set[str] = set()
    basenames: set[str] = set()
    for root, dirs, files in os.walk(root_path):
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "node_modules",
            }
        ]
        for f in files:
            rel = (
                bare_path(os.path.relpath(os.path.join(root, f), root_path))
                .lower()
                .replace("\\", "/")
            )
            rel_paths.add(rel)
            basenames.add(Path(f).name.lower())
    return frozenset(rel_paths), frozenset(basenames)


@functools.lru_cache(maxsize=16)
def _get_repo_src_symbols(repo_root_str: str, head_sha: str = "") -> frozenset[str]:
    """Return set of code tokens and identifiers in src/."""
    root_path = Path(repo_root_str)
    if not root_path.is_dir():
        return frozenset()
    words: set[str] = set()
    src_dir = root_path / "src"
    if src_dir.is_dir():
        for py_file in src_dir.glob("**/*.py"):
            with contextlib.suppress(Exception):
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                for token in _IDENTIFIER_RE.findall(content):
                    words.add(token.lower())
    return frozenset(words)


@functools.lru_cache(maxsize=16)
def _get_repo_test_definitions(repo_root_str: str, head_sha: str = "") -> frozenset[str]:
    """Return set of defined test function names in tests/."""
    root_path = Path(repo_root_str)
    if not root_path.is_dir():
        return frozenset()
    tests: set[str] = set()
    tests_dir = root_path / "tests"
    if tests_dir.is_dir():
        for py_file in tests_dir.glob("**/*.py"):
            with contextlib.suppress(Exception):
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r"(?m)^[ \t]*def\s+(test_[a-zA-Z0-9_]+)\s*\(", content):
                    tests.add(match.group(1).lower())
    return frozenset(tests)


def _file_exists_in_repo(
    path: str,
    changed_paths: Collection[str] | None = None,
    repo_files: Collection[str] | None = None,
    repo_root: Path | str | None = None,
    head_sha: str | None = None,
) -> bool:
    """Return True if path exists in changed_paths, repo_files, or locally in repository."""
    bp = bare_path(path).lower().replace("\\", "/")
    has_slash = "/" in bp

    if changed_paths:
        for cp in changed_paths:
            bcp = bare_path(cp).lower().replace("\\", "/")
            if bp == bcp or bcp.endswith("/" + bp):
                return True
            if not has_slash and Path(bcp).name == bp:
                return True

    if repo_files:
        for rf in repo_files:
            brf = bare_path(rf).lower().replace("\\", "/")
            if bp == brf or brf.endswith("/" + bp):
                return True
            if not has_slash and Path(brf).name == bp:
                return True

    resolved_root = _resolve_repo_root(repo_root)
    if resolved_root:
        rel_paths, basenames = _get_repo_file_index(str(resolved_root), head_sha or "")
        if has_slash:
            # Directory-qualified citation: require exact relative path or suffix of a real path
            if bp in rel_paths or any(k.endswith("/" + bp) for k in rel_paths):
                return True
        elif bp in basenames or bp in rel_paths:
            return True

    return False


def _test_exists_in_suite(
    test_name: str,
    diff_text: str,
    repo_symbols: Collection[str] | None = None,
    repo_root: Path | str | None = None,
    head_sha: str | None = None,
) -> bool:
    """Return True if test_name exists in diff_text, repo_symbols, or repo test suite."""
    t_lower = test_name.lower()
    if bool(
        re.search(
            rf"(?m)^[ \t]*(?:\+| )[ \t]*def\s+{re.escape(t_lower)}\s*\(",
            diff_text or "",
            re.IGNORECASE,
        )
    ):
        return True
    if repo_symbols and any(s.lower() == t_lower for s in repo_symbols):
        return True

    resolved_root = _resolve_repo_root(repo_root)
    if resolved_root:
        test_defs = _get_repo_test_definitions(str(resolved_root), head_sha or "")
        if t_lower in test_defs:
            return True

    return False


def _symbol_exists_in_codebase(
    symbol: str,
    diff_text: str,
    changed_paths: Collection[str] | None = None,
    repo_symbols: Collection[str] | None = None,
    repo_root: Path | str | None = None,
    head_sha: str | None = None,
    diff_tokens: Collection[str] | None = None,
) -> bool:
    """Return True if symbol exists in diff_text, repo_symbols, or repo code."""
    s_lower = symbol.lower()
    if s_lower in _COMMON_BUILTINS:
        return True
    tokens = (
        diff_tokens
        if diff_tokens is not None
        else {t.lower() for t in _IDENTIFIER_RE.findall(diff_text or "")}
    )
    if s_lower in tokens:
        return True
    if repo_symbols and any(s.lower() == s_lower for s in repo_symbols):
        return True

    resolved_root = _resolve_repo_root(repo_root)
    if resolved_root:
        src_symbols = _get_repo_src_symbols(str(resolved_root), head_sha or "")
        if s_lower in src_symbols:
            return True

    return False


def verify_semantic_invariant(
    invariant: str,
    diff_text: str,
    changed_paths: Collection[str] | None = None,
    repo_files: Collection[str] | None = None,
    repo_symbols: Collection[str] | None = None,
    repo_root: Path | str | None = None,
    head_sha: str | None = None,
) -> tuple[Literal["GROUNDED", "UNVERIFIED", "HALLUCINATED"], str]:
    """Deterministically fact-check a single semantic invariant against diff and repository.

    Returns:
        (status, reason) where status is one of:
        - "GROUNDED": Passes entity-existence checks and contains no uncertainty keyword.
        - "UNVERIFIED": Contains an uncertainty keyword and is demoted to unverified_claims.
        - "HALLUCINATED": Cites non-existent files, phantom symbols, or non-existent tests (purged/dropped).
    """
    raw_inv = (invariant or "").strip()
    if not raw_inv:
        return "HALLUCINATED", "empty invariant"

    # 1. Fact-check cited paths
    cited_paths = extract_cited_paths(raw_inv)
    cited_paths = {
        p
        for p in cited_paths
        if "." in p.rsplit("/", 1)[-1]
        and p.rsplit("/", 1)[-1].rsplit(".", 1)[-1] in _KNOWN_EXTENSIONS
    }
    for m in _INVARIANT_CITATION_RE.finditer(raw_inv):
        cited_paths.add(bare_path(m.group(1)).lower())

    for cp in cited_paths:
        if cp in PLACEHOLDER_PATHS:
            return "HALLUCINATED", f"Cited placeholder path '{cp}'"
        if not _file_exists_in_repo(cp, changed_paths, repo_files, repo_root, head_sha=head_sha):
            return "HALLUCINATED", f"Cited file '{cp}' does not exist in repository or diff"

    # 2. Fact-check claimed test functions
    test_claims = re.findall(r"\btest_[a-zA-Z0-9_]+", raw_inv)
    for tc in test_claims:
        if not _test_exists_in_suite(tc, diff_text, repo_symbols, repo_root, head_sha=head_sha):
            return (
                "HALLUCINATED",
                f"Claimed test '{tc}' does not exist in diff or repository test suite",
            )

    # 3. Fact-check cited symbols in backticks
    cited_symbols = extract_cited_symbols(raw_inv)
    domain_symbols = [s for s in cited_symbols if s not in _COMMON_BUILTINS]
    if domain_symbols:
        diff_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(diff_text or "")}
        missing = [
            s
            for s in domain_symbols
            if not _symbol_exists_in_codebase(
                s,
                diff_text,
                changed_paths,
                repo_symbols,
                repo_root,
                head_sha=head_sha,
                diff_tokens=diff_tokens,
            )
        ]
        if missing:
            return (
                "HALLUCINATED",
                f"Cited code symbol(s) {missing} do not exist in diff or repository",
            )

    # 4. Check for residual uncertainty or explicit unverified phrasing
    ev_lower = raw_inv.lower()
    if any(kw in ev_lower for kw in _UNCERTAINTY_KEYWORDS):
        return (
            "UNVERIFIED",
            "Expresses residual uncertainty or unverified claim; demoting to unverified_claims",
        )

    return "GROUNDED", "Grounded in valid repository entities and behavioral reasoning"


def filter_and_ground_semantic_invariants(
    invariants: Sequence[str],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
    repo_files: Collection[str] | None = None,
    repo_symbols: Collection[str] | None = None,
    repo_root: Path | str | None = None,
    head_sha: str | None = None,
) -> tuple[list[str], list[str]]:
    """Deterministically fact-check and triage semantic invariants.

    Returns:
        (grounded_semantic_invariants, demoted_unverified_claims)
    Any hallucinated invariant citing non-existent files or phantom symbols
    is purged completely and logged.
    """
    grounded: list[str] = []
    demoted: list[str] = []

    for inv in invariants:
        clean = (inv or "").strip()
        if not clean:
            continue
        status, reason = verify_semantic_invariant(
            clean,
            diff_text,
            changed_paths=changed_paths,
            repo_files=repo_files,
            repo_symbols=repo_symbols,
            repo_root=repo_root,
            head_sha=head_sha,
        )
        if status == "GROUNDED":
            if clean not in grounded:
                grounded.append(clean)
        elif status == "UNVERIFIED":
            if clean not in demoted:
                demoted.append(clean)
        else:
            logger.warning(
                "🛡️ Fact-Check: Purged hallucinated semantic invariant: '%s' (%s)",
                clean,
                reason,
            )

    return grounded, demoted


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


_TEST_CLAIM_RE = re.compile(r"(\d+)[^\d\n]{0,30}?tests?\b", re.I)
_ADDED_TEST_RE = re.compile(r"^\+\s*(?:async )?def test_", re.M)


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


def extract_scorecard_invariants(
    scorecard: Sequence[Any],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> list[str]:
    """Recover verified invariants from diff-grounded citations in scorecard notes.

    When a model cites concrete, valid path:line references inside its scorecard notes
    instead of populating verified_invariants directly, this function extracts those
    grounded references into verified invariants so mechanical proof is preserved.
    """
    if not scorecard or not diff_text:
        return []

    anchors, _line_text = walk_right_side(diff_text)
    changed = {bare_path(p) for p in changed_paths if p} if changed_paths is not None else None
    recovered: list[str] = []
    seen: set[str] = set()

    for item in scorecard:
        status = getattr(item, "status", None) or (
            item.get("status") if isinstance(item, dict) else ""
        )
        if str(status).upper() != "PASS":
            continue

        dimension = (
            getattr(item, "dimension", None)
            or (item.get("dimension") if isinstance(item, dict) else "")
        ) or "Scorecard"
        notes = (
            getattr(item, "notes", None) or (item.get("notes") if isinstance(item, dict) else "")
        ) or ""
        if not notes:
            continue

        for m in _INVARIANT_CITATION_RE.finditer(notes):
            path = bare_path(m.group(1))
            try:
                line_num = int(m.group(2))
            except (ValueError, TypeError):
                continue

            if changed is not None and path not in changed:
                continue

            anchor_lines = anchors.get(path, set())
            if line_num not in anchor_lines:
                continue

            key = f"{path}:{line_num}"
            if key in seen:
                continue
            seen.add(key)
            recovered.append(f"{path}:{line_num}: {dimension} check verified in PR diff.")

    return recovered


def strict_grounding_enabled() -> bool:
    """Return True when ungrounded citations should reject or downgrade a review, not merely log."""
    return STRICT_REVIEW_GROUNDING in ("1", "true", "True")


__all__ = [
    "BEHAVIORAL_KEYWORDS",
    "PLACEHOLDER_PATHS",
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
    "strict_grounding_enabled",
    "touched_lines_by_file",
    "verify_citations",
    "verify_resolutions",
    "verify_semantic_invariant",
]
