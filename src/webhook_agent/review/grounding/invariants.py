"""Invariant grounding, mechanical verification, and hallucination fact-checking.

Verifies model-asserted invariants against the diff and repository, partitioning them
into mechanically proven (Machine-Verified) and semantically asserted, while filtering
out unverified claims or purged hallucinations.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.grounding.citations import (
    _BACKTICK_RE,
    _IDENTIFIER_RE,
    _INVARIANT_CITATION_RE,
    _KNOWN_EXTENSIONS,
    _KNOWN_STDLIB_MODULE_PREFIXES,
    PLACEHOLDER_PATHS,
    _is_path_citation,
    _norm_evidence,
    bare_path,
)
from webhook_agent.review.grounding.heuristics import (
    extract_bare_symbols,
    extract_cited_paths,
    extract_cited_symbols,
)
from webhook_agent.tools.diff_tools import walk_right_side

logger = logging.getLogger("webhook_agent.review.grounding")

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

_INVARIANT_OPERATIVE_VERBS: frozenset[str] = frozenset(
    {
        "ensures",
        "ensure",
        "ensuring",
        "maintains",
        "maintain",
        "maintaining",
        "prevents",
        "prevent",
        "preventing",
        "guarantees",
        "guarantee",
        "guaranteeing",
        "enforces",
        "enforce",
        "enforcing",
        "requires",
        "require",
        "requiring",
        "handles",
        "handle",
        "handling",
        "validates",
        "validate",
        "validating",
        "rejects",
        "reject",
        "rejecting",
        "drops",
        "drop",
        "dropping",
        "allows",
        "allow",
        "allowing",
        "disallows",
        "disallow",
        "disallowing",
        "restricts",
        "restrict",
        "restricting",
        "coordinates",
        "coordinate",
        "coordinating",
        "triggers",
        "trigger",
        "triggering",
        "preserves",
        "preserve",
        "preserving",
        "preserved",
        "isolates",
        "isolate",
        "isolating",
        "executes",
        "execute",
        "executing",
        "delegates",
        "delegate",
        "delegating",
        "retains",
        "retain",
        "retaining",
        "maps",
        "map",
        "mapping",
        "survives",
        "survive",
        "surviving",
        "terminates",
        "terminate",
        "terminating",
        "synchronizes",
        "synchronize",
        "synchronizing",
        "protects",
        "protect",
        "protecting",
        "provides",
        "provide",
        "providing",
        "guards",
        "guard",
        "guarding",
        "bounds",
        "bound",
        "bounding",
        "limits",
        "limit",
        "limiting",
        "ignores",
        "ignore",
        "ignoring",
        "skips",
        "skip",
        "skipping",
        "filters",
        "filter",
        "filtering",
        "tracks",
        "track",
        "tracking",
        "blocks",
        "block",
        "blocking",
        "avoids",
        "avoid",
        "avoiding",
        "mitigates",
        "mitigate",
        "mitigating",
        "asserts",
        "assert",
        "asserting",
        "asserted",
        "prevented",
        "guaranteed",
        "enforced",
        "required",
        "handled",
        "validated",
        "rejected",
        "dropped",
        "allowed",
        "disallowed",
        "restricted",
        "coordinated",
        "triggered",
        "isolated",
        "executed",
        "delegated",
        "retained",
        "mapped",
        "survived",
        "terminated",
        "synchronized",
        "protected",
        "provided",
        "guarded",
        "bounded",
        "limited",
        "ignored",
        "skipped",
        "filtered",
        "tracked",
        "blocked",
        "avoided",
        "mitigated",
        "scopes",
        "scope",
        "scoping",
        "scoped",
        "eliminates",
        "eliminate",
        "eliminating",
        "eliminated",
        "constrains",
        "constrain",
        "constraining",
        "constrained",
        "propagates",
        "propagate",
        "propagating",
        "propagated",
        "intercepts",
        "intercept",
        "intercepting",
        "intercepted",
        "routes",
        "route",
        "routing",
        "routed",
        "dispatches",
        "dispatch",
        "dispatching",
        "dispatched",
        "sanitizes",
        "sanitize",
        "sanitizing",
        "sanitized",
        "standardizes",
        "standardize",
        "standardizing",
        "standardized",
        "quarantines",
        "quarantine",
        "quarantining",
        "quarantined",
        "shields",
        "shield",
        "shielding",
        "shielded",
        "restores",
        "restore",
        "restoring",
        "restored",
    }
)


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
       - If asserting behavioral logic, it contains a concrete code quote from the diff,
         cites a test function (test_\\w+), or matches code tokens on an anchored/snapped diff line.
       - The cited line number matches or falls within hunk snapping tolerance (<= 10 lines)
         of a right-side diff hunk in that file.
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

        known_lines = line_text.get(path, {})
        if line_num is not None:
            if known_lines:
                if line_num not in known_lines:
                    closest_dist = min(abs(line_num - kl) for kl in known_lines)
                    if closest_dist > 10:
                        return False
            else:
                return False

        raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(raw_inv) if q.strip()]
        single_quotes = [
            q.strip()
            for q in re.findall(r"'([A-Za-z0-9_]{2,})'", raw_inv)
            if q.strip() and q.lower() not in _COMMON_BUILTINS
        ]
        raw_quotes.extend(single_quotes)
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

        # Check if invariant references identifiers present on the anchor lines
        has_anchor_token_match = False
        if known_lines:
            target_lines = (
                [line_num]
                if (line_num is not None and line_num in known_lines)
                else (
                    [kl for kl in known_lines if abs(line_num - kl) <= 10]
                    if line_num is not None
                    else list(known_lines.keys())
                )
            )
            anchor_tokens = {
                t.lower()
                for kl in target_lines
                for t in _IDENTIFIER_RE.findall(known_lines.get(kl, ""))
            }
            inv_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(raw_inv)} - _COMMON_BUILTINS
            has_anchor_token_match = bool(anchor_tokens & inv_tokens)

        has_concrete_quote_or_test = has_diff_quote or has_test or has_anchor_token_match
        is_static_fact = re.search(r"^[A-Z0-9_]+\s*=\s*", raw_inv) is not None

        return not (is_behavioral and not is_static_fact and not has_concrete_quote_or_test)

    # No path:line citation: check if it directly quotes diff code or cites a test function in the diff
    raw_quotes = [q.strip() for q in _BACKTICK_RE.findall(raw_inv) if q.strip()]
    single_quotes = [
        q.strip()
        for q in re.findall(r"'([A-Za-z0-9_]{2,})'", raw_inv)
        if q.strip() and q.lower() not in _COMMON_BUILTINS
    ]
    raw_quotes.extend(single_quotes)
    code_quotes = [q for q in raw_quotes if not _is_path_citation(q)]
    has_matching_quote = any(
        len(_norm_evidence(q)) >= 16 and _norm_evidence(q) in norm_diff for q in code_quotes
    )
    has_test = bool(re.search(r"\btest_\w+", raw_inv)) and any(
        t in norm_diff for t in re.findall(r"\btest_\w+", raw_inv.lower())
    )
    return has_matching_quote or has_test


def promote_semantic_to_verified(
    semantic_invariants: Sequence[str],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Examine semantic invariants and promote any that possess mechanical proof.

    When the model places an invariant into semantic_invariants that actually satisfies
    mechanical verification requirements (e.g. valid path:line citation with diff proof,
    or verifiable code quote/test citation), this function promotes it to verified_invariants.

    Returns:
        (promoted_verified, remaining_semantic)
    """
    if not semantic_invariants or not diff_text:
        return [], list(semantic_invariants)

    promoted: list[str] = []
    remaining: list[str] = []

    for inv in semantic_invariants:
        clean = (inv or "").strip()
        if not clean:
            continue
        if is_machine_verified_invariant(clean, diff_text, changed_paths):
            promoted.append(clean)
        else:
            remaining.append(clean)

    return promoted, remaining


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


def enforce_invariant_dominance(
    verified_invariants: Sequence[str],
    semantic_invariants: Sequence[str],
    max_semantic: int = 2,
    is_code_pr: bool = True,
) -> tuple[list[str], list[str], list[str]]:
    """Enforce machine-proven invariant dominance and hard-cap semantic claims.

    Rules:
    1. semantic_invariants is capped at max_semantic (default 2).
    2. On code PRs (is_code_pr=True), verified_invariants must dominate:
       allowed_semantic = min(max_semantic, len(verified_invariants)).
       On non-code PRs, allowed_semantic = max_semantic.
    Any excess semantic invariants are demoted to unverified_claims.

    Returns:
        (retained_verified, retained_semantic, demoted_excess)
    """
    retained_verified = [inv.strip() for inv in verified_invariants if (inv or "").strip()]
    retained_semantic: list[str] = []
    demoted_excess: list[str] = []

    if is_code_pr:
        allowed_semantic = min(max_semantic, len(retained_verified))
    else:
        allowed_semantic = max_semantic

    for idx, inv in enumerate(semantic_invariants):
        clean = (inv or "").strip()
        if not clean:
            continue
        if idx < allowed_semantic:
            retained_semantic.append(clean)
        else:
            demoted_excess.append(clean)

    return retained_verified, retained_semantic, demoted_excess


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
        and not any(p.startswith(pfx) for pfx in _KNOWN_STDLIB_MODULE_PREFIXES)
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

    # 3. Fact-check cited symbols in backticks or code identifiers
    cited_symbols = extract_cited_symbols(raw_inv) | extract_bare_symbols(raw_inv)
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

    # 5. Require at least one operative invariant verb asserting an architectural property
    inv_words = {w.lower() for w in re.findall(r"\b[a-zA-Z]{3,}\b", raw_inv)}
    if not (inv_words & _INVARIANT_OPERATIVE_VERBS):
        return (
            "UNVERIFIED",
            "Lacks an operative invariant verb or architectural guarantee; demoting to unverified_claims",
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
