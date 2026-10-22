"""Deterministic repair of model-written line citations.

Reviews cite locations as ``path:line`` and usually quote the code they mean
right beside the number. That quote is the strong signal; the number is the weak
one, because the auditor reads files through tools that hand back unnumbered
text and then counts by hand. A one-or-two line drift is therefore routine even
when the quote is verbatim.

This module re-anchors each citation to its quote: find the snippet in the file,
and rewrite the number when it lives elsewhere within a small window. Ambiguous
or paraphrased quotes are left alone, never guessed at.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("webhook_agent.review.citation_repair")

__all__ = [
    "CitationRepair",
    "find_snippet_line",
    "repair_line_citations",
    "repair_rendered_citations",
    "review_file_loader",
]

DEFAULT_MAX_DRIFT = 20
_MAX_SNIPPET_CHARS = 240
_BACKWARD_WINDOW = 160

CITATION_RE = re.compile(
    r"(?P<path>(?:[\w.\-]+/)*[\w.\-]+\.(?:py|pyi|md|yml|yaml|toml|json|cfg|ini|txt))"
    r":(?P<line>\d{1,6})"
)
_QUOTED_RE = re.compile(rf"[`'\"](?P<snippet>[^`'\"\n]{{8,{_MAX_SNIPPET_CHARS}}})[`'\"]")


@dataclass(frozen=True)
class CitationRepair:
    """A single citation whose line number was rewritten."""

    path: str
    original_line: int
    repaired_line: int


def _normalize(text: str) -> str:
    """Collapse whitespace so indentation differences never block a match."""
    return " ".join(text.split())


def _looks_like_code(snippet: str) -> bool:
    """Return True when a quoted fragment looks like code rather than prose or a URL."""
    if snippet.startswith(("http://", "https://")):
        return False
    return any(char in snippet for char in "()=:[]{}>")


def _nearest_snippet(text: str, match: re.Match[str]) -> str | None:
    """Return the code fragment quoted closest to a citation, if any."""
    line_end = text.find("\n", match.end())
    forward_end = line_end if line_end != -1 else match.end() + _MAX_SNIPPET_CHARS
    for candidate in _QUOTED_RE.finditer(text[match.end() : forward_end]):
        snippet = candidate.group("snippet").strip()
        if _looks_like_code(snippet):
            return snippet

    backward_start = max(0, match.start() - _BACKWARD_WINDOW)
    found: str | None = None
    for candidate in _QUOTED_RE.finditer(text[backward_start : match.start()]):
        snippet = candidate.group("snippet").strip()
        if _looks_like_code(snippet):
            found = snippet
    return found


def _load_lines(
    path: str,
    loader: Callable[[str], str | None],
    cache: dict[str, list[str] | None],
) -> list[str] | None:
    """Load and memoise a file's lines, tolerating loader failures."""
    if path in cache:
        return cache[path]
    content: str | None = None
    try:
        content = loader(path)
    except Exception as err:  # pragma: no cover - defensive around external loaders
        logger.debug("Citation loader failed for %s: %s", path, err)
    lines = content.splitlines() if isinstance(content, str) else None
    cache[path] = lines
    return lines


def _window_starts(lines: Sequence[str], needle: str, max_window: int = 8) -> list[int]:
    """Return start lines where a multi-line quote spans consecutive lines."""
    first_token = needle.split(" ", 1)[0]
    if not first_token:
        return []
    starts: list[int] = []
    for index, raw_line in enumerate(lines):
        if first_token not in _normalize(raw_line):
            continue
        parts: list[str] = []
        for offset in range(max_window):
            if index + offset >= len(lines):
                break
            part = _normalize(lines[index + offset])
            if part:
                parts.append(part)
            joined = " ".join(parts)
            # Require the quote to be fully present; a window that is merely a
            # fragment of the quote belongs to a later start line.
            if joined and needle in joined:
                starts.append(index + 1)
                break
    return starts


def find_snippet_line(
    lines: Sequence[str],
    snippet: str,
    anchor: int,
    max_drift: int = DEFAULT_MAX_DRIFT,
) -> int | None:
    """Locate ``snippet`` in ``lines``, preferring the occurrence nearest ``anchor``.

    Handles both single-line fragments ("``return None``") and quotes that span
    several lines, which the auditor produces when it copies a whole statement.
    Multi-line quotes resolve to the line where the quote *starts*.

    Args:
        lines: File contents split into lines.
        snippet: Quoted code fragment taken from the review.
        anchor: The line number the review claimed.
        max_drift: How far the snippet may legitimately have drifted.

    Returns:
        1-based line number where the snippet starts, or None when nothing matches
        or several distant occurrences make the choice ambiguous.
    """
    needle = _normalize(snippet)
    if not needle:
        return None

    normalized = [_normalize(line) for line in lines]

    # 1. A single line that fully contains the quote.
    matches = [index for index, line in enumerate(normalized, start=1) if line and needle in line]

    # 2. A quote spanning consecutive lines; report where it starts.
    if not matches:
        matches = _window_starts(lines, needle)

    # 3. Last resort: a single line that the quote merely begins with.
    if not matches:
        matches = [
            index
            for index, line in enumerate(normalized, start=1)
            if len(line) >= 20 and line in needle
        ]

    if not matches:
        return None

    nearby = [line for line in matches if abs(line - anchor) <= max_drift]
    if nearby:
        return min(nearby, key=lambda line: abs(line - anchor))

    # A single occurrence anywhere in the file is still unambiguous evidence.
    if len(matches) == 1:
        return matches[0]

    return None


def repair_line_citations(
    text: str,
    loader: Callable[[str], str | None],
    max_drift: int = DEFAULT_MAX_DRIFT,
) -> tuple[str, list[CitationRepair]]:
    """Rewrite ``path:line`` citations that disagree with the code quoted beside them.

    Args:
        text: Rendered review markdown.
        loader: Returns file contents for a repo-relative path, or None.
        max_drift: Maximum line distance considered a fixable slip.

    Returns:
        The (possibly rewritten) text and the repairs that were applied.
    """
    if not text:
        return text, []

    cache: dict[str, list[str] | None] = {}
    replacements: list[tuple[int, int, int]] = []
    repairs: list[CitationRepair] = []

    for match in CITATION_RE.finditer(text):
        path = match.group("path")
        try:
            original = int(match.group("line"))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue

        snippet = _nearest_snippet(text, match)
        if snippet is None:
            continue

        lines = _load_lines(path, loader, cache)
        if not lines or original > len(lines):
            continue

        repaired = find_snippet_line(lines, snippet, original, max_drift)
        if repaired is None or repaired == original:
            continue

        replacements.append((match.start("line"), match.end("line"), repaired))
        repairs.append(CitationRepair(path, original, repaired))

    if not replacements:
        return text, []

    pieces: list[str] = []
    cursor = 0
    for start, end, new_line in replacements:
        pieces.append(text[cursor:start])
        pieces.append(str(new_line))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), repairs


def review_file_loader(pr: Any = None) -> Callable[[str], str | None]:
    """Build a file loader for citation repair.

    Prefers the active PR workspace (a local checkout at the analysed commit) and
    falls back to the GitHub contents API at the PR head when no workspace is set.
    """
    from webhook_agent.environment.context import get_active_environment

    env = get_active_environment()
    cache: dict[str, str | None] = {}
    head_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")

    def load(path: str) -> str | None:
        if path in cache:
            return cache[path]

        content: str | None = None
        if env is not None:
            target = env.resolve_path(path)
            if target is not None and target.is_file():
                with contextlib.suppress(OSError):
                    content = target.read_text(encoding="utf-8", errors="replace")

        if content is None and pr is not None:
            with contextlib.suppress(Exception):
                repo = pr.head.repo
                fetched = repo.get_contents(path, ref=head_sha or None)
                if not isinstance(fetched, list):
                    content = fetched.decoded_content.decode("utf-8", errors="replace")

        cache[path] = content
        return content

    return load


def repair_rendered_citations(
    body: str,
    pr: Any = None,
    max_drift: int = DEFAULT_MAX_DRIFT,
) -> str:
    """Repair citations in a rendered review body, logging every rewrite."""
    repaired_body, repairs = repair_line_citations(body, review_file_loader(pr), max_drift)
    for repair in repairs:
        logger.info(
            "🔧 Citation repaired: %s:%d → %d",
            repair.path,
            repair.original_line,
            repair.repaired_line,
        )
    return repaired_body
