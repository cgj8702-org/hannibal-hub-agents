"""Duplicate review comment suppression, Jaccard token similarity, and repeat grouping.

Adapted directly from adk-samples/.github/scripts/post_review_comments.py for hannibal-hub-agents.
"""

from __future__ import annotations

import re
from typing import Any

PROXIMITY = 2
MIN_SIMILARITY = 0.55

_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "and",
    "any",
    "are",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "cannot",
    "could",
    "did",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "here",
    "how",
    "into",
    "its",
    "itself",
    "more",
    "most",
    "nor",
    "not",
    "off",
    "once",
    "only",
    "other",
    "ought",
    "our",
    "ours",
    "out",
    "over",
    "own",
    "same",
    "should",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "too",
    "under",
    "until",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "with",
    "would",
}

_GROUP_NOTE = re.compile(r"\n*\(Same thing in \d+ other places? in this review\.\)\s*$")
TAG_PREFIX = re.compile(r"^\[(?:CRITICAL|MAJOR|MINOR)\]\s*", re.IGNORECASE)
DEFANG_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def defang_content(text: str) -> str:
    """Disarm markdown image links so untrusted diff text cannot embed external images."""
    return DEFANG_IMAGE.sub(r"[\1](\2)", str(text or ""))


def _tokens(text: str) -> set[str]:
    """Tokenize text into lowercased words without stopwords."""
    return {
        word
        for word in re.findall(r"[a-z_][a-z_0-9]{2,}", str(text).lower())
        if word not in _STOPWORDS
    }


def _similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity across token sets: |a & b| / |a | b|."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _base_body(text: str) -> str:
    """Reduce a comment body by stripping severity tags and repeat grouping notes."""
    stripped = _GROUP_NOTE.sub("", str(text or ""))
    return TAG_PREFIX.sub("", stripped).strip()


def build_exclusions(
    existing: list[dict[str, Any]],
) -> tuple[dict[str, dict[int, dict[str, Any]]], list[tuple[set[str], dict[str, Any]]]]:
    """Build spatial line zones (±PROXIMITY lines) and tokenized bodies from past comments.

    Returns:
        (zones, texts)
        - zones: path -> line -> comment_dict
        - texts: list of (token_set, comment_dict)
    """
    zones: dict[str, dict[int, dict[str, Any]]] = {}
    texts: list[tuple[set[str], dict[str, Any]]] = []

    for comment in existing or []:
        body = str(comment.get("body") or "").strip()
        if body:
            base = _base_body(body)
            texts.append((_tokens(base), comment))

        path = comment.get("path")
        line = comment.get("line") or comment.get("original_line")
        if path and isinstance(line, int):
            for delta in range(-PROXIMITY, PROXIMITY + 1):
                zones.setdefault(path, {}).setdefault(line + delta, comment)

    return zones, texts


def already_raised(
    path: str,
    line: int | None,
    body: str,
    zones: dict[str, dict[int, dict[str, Any]]],
    texts: list[tuple[set[str], dict[str, Any]]],
    trusted: bool = False,
) -> str:
    """Check if this finding repeats something already posted on this PR.

    Returns:
        Explanation reason if already raised, or empty string "" if new.
    """
    cleaned_body = _base_body(body)
    tokens = _tokens(cleaned_body)

    # 1. Check exact line proximity match
    if line is not None and not trusted:
        existing_line_comment = zones.get(path, {}).get(line)
        if existing_line_comment:
            return f"already commented near {path}:{line}"

    # 2. Check token similarity across previous comments
    if tokens:
        for prev_tokens, prev_comment in texts:
            if not prev_tokens:
                continue
            sim = _similarity(tokens, prev_tokens)
            if sim >= MIN_SIMILARITY:
                prev_line = prev_comment.get("line") or "<general>"
                prev_path = prev_comment.get("path") or "<review-body>"
                return f"similar ({sim:.0%}) to earlier comment on {prev_path}:{prev_line}"

    return ""


def group_repeated_findings(
    issues: list[Any],
    threshold: int = 3,
) -> list[Any]:
    """If 3 or more issues share the same defect class, group them into ONE comment with a count."""
    if len(issues) < threshold:
        return issues

    def _get_desc(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("description", ""))
        return str(getattr(item, "description", ""))

    # Cluster issues by description token similarity
    clusters: list[list[Any]] = []
    for issue in issues:
        desc = _get_desc(issue)
        tokens = _tokens(desc)
        placed = False
        for cluster in clusters:
            leader_tokens = _tokens(_get_desc(cluster[0]))
            if _similarity(tokens, leader_tokens) >= 0.6:
                cluster.append(issue)
                placed = True
                break
        if not placed:
            clusters.append([issue])

    grouped_issues: list[Any] = []
    for cluster in clusters:
        if len(cluster) >= threshold:
            leader = cluster[0]
            count = len(cluster)
            extra_note = f"\n\n(Same thing in {count - 1} other places in this review.)"
            if isinstance(leader, dict):
                leader_copy = dict(leader)
                leader_copy["description"] = f"{leader_copy.get('description', '')}{extra_note}"
                grouped_issues.append(leader_copy)
            elif hasattr(leader, "model_copy"):
                leader_copy = leader.model_copy(
                    update={"description": f"{getattr(leader, 'description', '')}{extra_note}"}
                )
                grouped_issues.append(leader_copy)
            else:
                grouped_issues.append(leader)
        else:
            grouped_issues.extend(cluster)

    return grouped_issues
