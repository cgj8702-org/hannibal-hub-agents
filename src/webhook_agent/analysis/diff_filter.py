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
