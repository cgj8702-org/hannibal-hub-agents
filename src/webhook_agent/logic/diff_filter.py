"""Diff filtering, unreviewable file suppression, and reviewable churn metrics.

Adapted directly from adk-samples/.github/scripts/prepare_review_diff.py for hannibal-hub-agents.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# What never earns a review comment: (pattern, reason), matched
# case-insensitively against the full path.
SKIP_PATTERNS = [
    (
        r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock"
        r"|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|uv\.lock)$",
        "lockfile",
    ),
    (r"(^|/)(vendor|node_modules|third_party|external)/", "vendored"),
    (r"(^|/)(dist|build|out|target)/", "build output"),
    (r"(^|/)__snapshots__/", "snapshot"),
    (r"\.snap$", "snapshot"),
    (r"\.min\.(js|css)$", "minified"),
    (r"\.(pb|pb2)\.(go|py|js|ts|cc|h)$", "generated protobuf"),
    (r"_pb2(_grpc)?\.pyi?$", "generated protobuf"),
    (r"\.generated\.[a-z]+$", "generated"),
    (r"(^|/)generated/", "generated"),
    (
        r"\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|tar|gz|jar"
        r"|so|dylib|dll)$",
        "binary asset",
    ),
    (r"(^|/)testdata/", "test fixture"),
]

BULK_DATA_EXT = {
    ".json",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".xml",
    ".sql",
    ".txt",
    ".ndjson",
    ".jsonl",
}
BULK_DATA_CHURN = 500

BUDGET_TABLE = [
    (50, 2),
    (200, 3),
    (math.inf, 5),
]

FILE_HEADER = re.compile(r"^diff --git ")
C_ESCAPE = re.compile(r"\\(?:([\\\"abfnrtv])|([0-7]{3}))")
_C_SIMPLE = {
    "\\": b"\\",
    '"': b'"',
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
}


def _unquote_git_path(raw: str) -> str:
    """A path as git printed it, turned back into the real name."""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    try:
        out = bytearray()
        index = 0
        for match in C_ESCAPE.finditer(body):
            out.extend(body[index : match.start()].encode("utf-8"))
            simple, octal = match.group(1), match.group(2)
            out.extend(_C_SIMPLE[simple] if simple else bytes([int(octal, 8)]))
            index = match.end()
        out.extend(body[index:].encode("utf-8"))
        return out.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw


def _strip_side_prefix(path: str) -> str:
    """`a/x` or `b/x` -> `x`."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def skip_reason(path: str, churn: int = 0) -> str | None:
    """Why this file earns no review comment, or None if it is reviewable."""
    for pattern, reason in SKIP_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return reason
    suffix = Path(path).suffix.lower()
    if suffix in BULK_DATA_EXT and churn >= BULK_DATA_CHURN:
        return "bulk data"
    return None


def budget_for(churn: int) -> int:
    """Comments expected for this much churn."""
    for ceiling, budget in BUDGET_TABLE:
        if churn <= ceiling:
            return budget
    return BUDGET_TABLE[-1][1]


@dataclass
class DiffSection:
    path: str
    lines: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0

    @property
    def churn(self) -> int:
        return self.added + self.removed


@dataclass
class DiffFilterResult:
    filtered_diff: str
    reviewable_lines: int
    kept_files: list[str]
    skipped_files: dict[str, str]
    is_reviewable: bool
    budget: int


def split_diff_into_sections(diff_text: str) -> list[DiffSection]:
    """Parse a unified diff into per-file sections with churn counts."""
    sections: list[DiffSection] = []
    current: DiffSection | None = None

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            parts = line.strip().split()
            path = "unknown"
            if len(parts) >= 4:
                b_path = _unquote_git_path(parts[3])
                path = _strip_side_prefix(b_path)
            current = DiffSection(path=path, lines=[line])
            continue

        if line.startswith("File: ") and not line.startswith("File: context"):
            if current is not None:
                sections.append(current)
            file_part = line[6:].strip()
            # Handle "File: src/main.py (modified)"
            path = file_part.split(" (")[0].strip()
            current = DiffSection(path=path, lines=[line])
            continue

        if line.startswith("+++ "):
            target = line[4:].strip()
            parsed_path = "unknown" if target == "/dev/null" else _strip_side_prefix(target)
            if current is None:
                current = DiffSection(path=parsed_path, lines=[line])
                continue
            has_diff_git = any(sec_line.startswith("diff --git ") for sec_line in current.lines)
            if not has_diff_git and current.path != "unknown" and parsed_path != "unknown":
                sections.append(current)
                current = DiffSection(path=parsed_path, lines=[line])
                continue
            if current.path == "unknown" and parsed_path != "unknown":
                current.path = parsed_path
            current.lines.append(line)
            continue

        if current is None:
            continue

        current.lines.append(line)
        if line.startswith("+") and not line.startswith("+++"):
            current.added += 1
        elif line.startswith("-") and not line.startswith("---"):
            current.removed += 1

    if current is not None:
        sections.append(current)

    return sections


def filter_review_diff(
    diff_text: str,
    only_files: list[str] | set[str] | None = None,
    max_bytes: int = 125000,
) -> DiffFilterResult:
    """Filter out noise, vendored code, lockfiles, and non-PR changes from a diff.

    Args:
        diff_text: Raw unified diff string.
        only_files: Optional allowlist of files belonging to this PR (e.g., from PR API).
        max_bytes: Maximum byte size for the filtered diff string.

    Returns:
        DiffFilterResult with clean diff text, reviewable churn stats, and recommended comment budget.
    """
    if not diff_text or not diff_text.strip():
        return DiffFilterResult(
            filtered_diff="",
            reviewable_lines=0,
            kept_files=[],
            skipped_files={},
            is_reviewable=False,
            budget=0,
        )

    sections = split_diff_into_sections(diff_text)
    allowed_set = set(only_files) if only_files is not None else None

    kept_sections: list[DiffSection] = []
    skipped_files: dict[str, str] = {}
    kept_files: list[str] = []
    reviewable_lines = 0

    for sec in sections:
        # Check if excluded by PR file list
        if allowed_set is not None and sec.path not in allowed_set:
            skipped_files[sec.path] = "not in PR files"
            continue

        # Check skip patterns (lockfiles, vendored code, binary files)
        reason = skip_reason(sec.path, sec.churn)
        if reason is not None:
            skipped_files[sec.path] = reason
            continue

        kept_sections.append(sec)
        kept_files.append(sec.path)
        reviewable_lines += sec.churn

    # Assemble filtered diff up to max_bytes
    out_lines: list[str] = []
    current_bytes = 0
    for sec in kept_sections:
        sec_text = "".join(sec.lines)
        sec_bytes = len(sec_text.encode("utf-8"))
        if current_bytes + sec_bytes > max_bytes and current_bytes > 0:
            out_lines.append(
                f"\n[... remaining files truncated to fit {max_bytes} byte budget ...]\n"
            )
            break
        out_lines.append(sec_text)
        current_bytes += sec_bytes

    filtered_text = "".join(out_lines)
    is_reviewable = bool(kept_files and reviewable_lines > 0)
    budget = budget_for(reviewable_lines) if is_reviewable else 0

    return DiffFilterResult(
        filtered_diff=filtered_text,
        reviewable_lines=reviewable_lines,
        kept_files=kept_files,
        skipped_files=skipped_files,
        is_reviewable=is_reviewable,
        budget=budget,
    )
