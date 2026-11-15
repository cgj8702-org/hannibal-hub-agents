"""Diff filtering, unreviewable file suppression, and reviewable churn metrics.

Adapted directly from adk-samples/.github/scripts/prepare_review_diff.py for hannibal-hub-agents.
"""

from __future__ import annotations

import ast
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("webhook_agent.analysis.diff_filter")

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


def is_reviewable_path(path: str, churn: int = 0) -> bool:
    """Return True if a file path is eligible for review comments."""
    return skip_reason(path, churn=churn) is None


def reviewable_file_paths(paths: Iterable[str]) -> list[str]:
    """Filter an iterable of paths to only those eligible for review comments."""
    return [p for p in paths if is_reviewable_path(p)]


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
class EnclosingScopeBlock:
    """An enclosing AST function or class block surrounding a diff hunk."""

    file_path: str
    scope_name: str
    start_line: int
    end_line: int
    code: str


@dataclass
class DiffFilterResult:
    filtered_diff: str
    reviewable_lines: int
    kept_files: list[str]
    skipped_files: dict[str, str]
    is_reviewable: bool
    budget: int
    ast_risk_score: float = 0.0
    ast_impact_counts: dict[str, int] = field(default_factory=dict)
    enclosing_scopes: list[EnclosingScopeBlock] = field(default_factory=list)
    enclosing_scopes_markdown: str = ""


TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[a-z]+$", re.IGNORECASE)
PROSE_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}
MIN_PARTIAL_BYTES = 1000
TRUNCATED_MARKER = "[... this file was truncated here ...]"
TRUNCATED_MARKER_BYTES = len(TRUNCATED_MARKER.encode("utf-8")) + 2
OMISSION_NOTICE_BYTES = 256
HUNK_COUNTS = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def review_tier(path: str) -> int:
    """Classify file path into review priority tier: 0 source, 1 test, 2 prose."""
    if Path(path).suffix.lower() in PROSE_EXT:
        return 2
    if TEST_PATH.search(path):
        return 1
    return 0


def _hunk_is_complete(rows: list[str]) -> bool:
    """Return True if all added/removed lines declared in the hunk header survived."""
    header = HUNK_COUNTS.match(rows[0]) if rows else None
    if not header:
        return False
    want_old = int(header.group(1) or 1)
    want_new = int(header.group(2) or 1)
    old = new = 0
    for row in rows[1:]:
        if row.startswith("\\"):
            continue
        if row.startswith("+"):
            new += 1
        elif row.startswith("-"):
            old += 1
        else:
            old += 1
            new += 1
    return old >= want_old and new >= want_new


def _truncate_at_hunk(text: str, limit: int) -> str:
    """Truncate unified diff text to limit bytes, keeping hunk boundaries intact."""
    rows = text.splitlines(keepends=True)
    kept: list[str] = []
    size = 0
    for row in rows:
        cost = len(row.encode("utf-8"))
        if size + cost > limit:
            break
        kept.append(row)
        size += cost
    if len(kept) == len(rows):
        return text

    for index in range(len(kept) - 1, 0, -1):
        if kept[index].startswith("@@"):
            if index > 1 and not _hunk_is_complete(kept[index:]):
                kept = kept[:index]
            break
    return "".join(kept)


def _omission_notice(omitted: list[str], partial: list[str]) -> str:
    """Generate diagnostic warning for model about omitted/partially included files."""
    if not omitted and not partial:
        return ""
    parts = []
    if omitted:
        parts.append(f"{len(omitted)} file(s) omitted entirely")
    if partial:
        parts.append(f"{len(partial)} shown only in part")
    return (
        f"\n[... {', '.join(parts)} to fit the prompt budget. Review ONLY "
        f"what is shown above; say nothing about code you were not shown "
        f"...]\n"
    )


def pack_to_budget(sections: list[DiffSection], max_bytes: int) -> tuple[str, list[str], list[str]]:
    """Pack diff sections into max_bytes prioritizing source code, then tests, then docs.

    Returns:
        (packed_diff_text, partial_file_paths, omitted_file_paths)
    """
    if max_bytes <= 0:
        return "".join("".join(s.lines) for s in sections), [], []

    total_bytes = sum(len("".join(s.lines).encode("utf-8")) for s in sections)
    if total_bytes <= max_bytes:
        return "".join("".join(s.lines) for s in sections), [], []

    room = max_bytes - OMISSION_NOTICE_BYTES

    order = sorted(
        range(len(sections)),
        key=lambda i: (
            review_tier(sections[i].path),
            len("".join(sections[i].lines).encode("utf-8")),
            sections[i].path,
        ),
    )

    chosen: dict[int, str] = {}
    partial: list[str] = []
    dropped: list[int] = []

    for i in order:
        sec = sections[i]
        sec_text = "".join(sec.lines)
        cost = len(sec_text.encode("utf-8"))
        if cost <= room:
            chosen[i] = sec_text
            room -= cost
        elif room >= MIN_PARTIAL_BYTES and not partial:
            cut = _truncate_at_hunk(sec_text, room - TRUNCATED_MARKER_BYTES)
            if len(cut.encode("utf-8")) >= MIN_PARTIAL_BYTES:
                chosen[i] = f"{cut.rstrip()}\n{TRUNCATED_MARKER}\n"
                partial.append(sec.path)
                room = 0
            else:
                dropped.append(i)
        else:
            dropped.append(i)

    omitted = [sections[i].path for i in sorted(dropped)]

    rows: list[str] = [chosen[i] for i in sorted(chosen)]
    out = "".join(rows)
    notice = _omission_notice(omitted, partial)
    if notice:
        out += notice

    return out, partial, omitted


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


HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def extract_hunk_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Extract modified new-file line ranges `(start, end)` per file path from a unified diff."""
    sections = split_diff_into_sections(diff_text)
    file_ranges: dict[str, list[tuple[int, int]]] = {}
    for sec in sections:
        ranges: list[tuple[int, int]] = []
        for line in sec.lines:
            m = HUNK_HEADER.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                end = start + max(count, 1) - 1
                ranges.append((start, end))
        if ranges:
            file_ranges[sec.path] = ranges
    return file_ranges


class _ScopeCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.class_stack: list[str] = []
        self.scopes: list[tuple[int, int, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = ".".join([*self.class_stack, node.name])
        end = getattr(node, "end_lineno", node.lineno)
        self.scopes.append((node.lineno, end, f"class {qual}"))
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qual = ".".join([*self.class_stack, node.name])
        end = getattr(node, "end_lineno", node.lineno)
        self.scopes.append((node.lineno, end, f"def {qual}"))
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        qual = ".".join([*self.class_stack, node.name])
        end = getattr(node, "end_lineno", node.lineno)
        self.scopes.append((node.lineno, end, f"async def {qual}"))
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()


def extract_enclosing_ast_blocks(
    diff_text: str,
    repo_root: Path | str | None = None,
    max_lines_per_block: int = 50,
) -> list[EnclosingScopeBlock]:
    """Extract enclosing AST function/class definitions around modified diff hunks.

    For each modified hunk in a Python file, locates the narrowest enclosing AST
    scope (function, async function, or class) from the repository workspace and
    slices the surrounding code to provide clinical context without sending the entire file.
    """
    if not diff_text or not diff_text.strip():
        return []

    root_path: Path | None = Path(repo_root) if repo_root is not None else None
    if root_path is None or not root_path.exists():
        return []

    hunk_map = extract_hunk_ranges(diff_text)
    blocks: list[EnclosingScopeBlock] = []
    seen_scopes: set[tuple[str, int, int]] = set()

    for file_rel_path, ranges in hunk_map.items():
        if not file_rel_path.endswith(".py"):
            continue
        file_disk_path = root_path / file_rel_path
        if not file_disk_path.exists() or not file_disk_path.is_file():
            continue

        try:
            source = file_disk_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_disk_path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        source_lines = source.splitlines()
        collector = _ScopeCollector()
        collector.visit(tree)
        scopes = collector.scopes

        for h_start, h_end in ranges:
            enclosing: tuple[int, int, str] | None = None
            min_span = math.inf
            for s_start, s_end, s_name in scopes:
                if s_start <= h_start <= s_end:
                    span = s_end - s_start
                    if span < min_span:
                        min_span = span
                        enclosing = (s_start, s_end, s_name)

            if enclosing is not None:
                scope_start, scope_end, scope_name = enclosing
                scope_key = (file_rel_path, scope_start, scope_end)
                if scope_key in seen_scopes:
                    continue
                seen_scopes.add(scope_key)

                scope_slice = source_lines[scope_start - 1 : scope_end]
                if len(scope_slice) <= max_lines_per_block:
                    code_block = "\n".join(scope_slice)
                else:
                    head_lines = scope_slice[:3]
                    w_start = max(scope_start, h_start - 4)
                    w_end = min(scope_end, h_end + 4)
                    body_lines = source_lines[w_start - 1 : w_end]
                    code_block = (
                        "\n".join(head_lines)
                        + "\n    # ... [context omitted] ...\n"
                        + "\n".join(body_lines)
                    )

                blocks.append(
                    EnclosingScopeBlock(
                        file_path=file_rel_path,
                        scope_name=scope_name,
                        start_line=scope_start,
                        end_line=scope_end,
                        code=code_block,
                    )
                )

    return blocks


def format_enclosing_scopes_markdown(blocks: list[EnclosingScopeBlock]) -> str:
    """Format extracted enclosing AST scopes into clean GitHub Markdown."""
    if not blocks:
        return ""
    lines: list[str] = [
        "### 🧩 Enclosing AST Scopes for Modified Hunks\n"
        "The following enclosing function and class scopes were extracted directly from the repository "
        "to provide complete local behavioral context for modified lines without reading entire files:\n"
    ]
    for b in blocks:
        lines.append(
            f"#### `{b.file_path}` (`{b.scope_name}`, lines {b.start_line}-{b.end_line})\n"
            f"```python\n{b.code}\n```\n"
        )
    return "\n".join(lines)


def filter_review_diff(
    diff_text: str,
    only_files: list[str] | set[str] | None = None,
    max_bytes: int | None = None,
    repo_root: Path | str | None = None,
) -> DiffFilterResult:
    """Filter out noise, vendored code, lockfiles, and non-PR changes from a diff.

    Args:
        diff_text: Raw unified diff string.
        only_files: Optional allowlist of files belonging to this PR (e.g., from PR API).
        max_bytes: Maximum byte size for the filtered diff string.
            If None, resolves dynamically: 5,000,000 bytes on paid tier, 125,000 bytes on free tier.
        repo_root: Optional root directory of repository workspace to extract enclosing AST scopes.

    Returns:
        DiffFilterResult with clean diff text, reviewable churn stats, recommended comment budget,
        and surgical enclosing AST scope blocks.
    """
    if max_bytes is None:
        try:
            from webhook_agent.models.rate_limiter import _resolve_tier

            max_bytes = 5_000_000 if _resolve_tier() == "paid" else 125_000
        except Exception:
            max_bytes = 125_000

    if not diff_text or not diff_text.strip():
        return DiffFilterResult(
            filtered_diff="",
            reviewable_lines=0,
            kept_files=[],
            skipped_files={},
            is_reviewable=False,
            budget=0,
            ast_risk_score=0.0,
            ast_impact_counts={},
            enclosing_scopes=[],
            enclosing_scopes_markdown="",
        )

    sections = split_diff_into_sections(diff_text)
    allowed_set = set(only_files) if only_files is not None else None

    kept_sections: list[DiffSection] = []
    skipped_files: dict[str, str] = {}
    kept_files: list[str] = []
    reviewable_lines = 0

    for sec in sections:
        if allowed_set is not None and sec.path not in allowed_set:
            skipped_files[sec.path] = "not in PR files"
            continue

        reason = skip_reason(sec.path, sec.churn)
        if reason is not None:
            skipped_files[sec.path] = reason
            continue

        kept_sections.append(sec)
        kept_files.append(sec.path)
        reviewable_lines += sec.churn

    filtered_text, partial_files, omitted_files = pack_to_budget(kept_sections, max_bytes)
    for p in omitted_files:
        skipped_files[p] = f"budget limit ({max_bytes} bytes)"
    for p in partial_files:
        skipped_files[p] = "truncated to fit budget"
    is_reviewable = bool(kept_files and reviewable_lines > 0)
    budget = budget_for(reviewable_lines) if is_reviewable else 0

    scopes = extract_enclosing_ast_blocks(filtered_text, repo_root=repo_root)
    scopes_md = format_enclosing_scopes_markdown(scopes)

    return DiffFilterResult(
        filtered_diff=filtered_text,
        reviewable_lines=reviewable_lines,
        kept_files=kept_files,
        skipped_files=skipped_files,
        is_reviewable=is_reviewable,
        budget=budget,
        ast_risk_score=0.0,
        ast_impact_counts={},
        enclosing_scopes=scopes,
        enclosing_scopes_markdown=scopes_md,
    )
