"""Window matching and ASCII skeleton comparison for Critic verification.

Adapted from google/adk-samples/.github/scripts/post_review_comments.py.
Validates quoted diff lines (window) against right-side diff lines or disk content.
If line arithmetic is slightly off (+/- MAX_DRIFT), automatically snaps and corrects
the line number rather than discarding the finding. If completely mismatched or
fabricated, flags the finding as falsified.
"""

from __future__ import annotations

import re
import unicodedata

WINDOW_LINE = re.compile(r"^\s*(\d{1,12})\s*[:|]\s?(.*)$")
ESCAPE_SEQ = re.compile(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\x[0-9a-fA-F]{2}")
MIN_PREFIX = 6
MAX_DRIFT = 5


def norm(text: str) -> str:
    """Reduce a source line to a comparable ASCII skeleton.

    Drops escape sequences, normalizes unicode (NFKC), and discards non-ASCII and whitespace.
    """
    text = text or ""
    text = ESCAPE_SEQ.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c.isascii() and not c.isspace())


class WindowMatcher:
    """Verifies that a finding's quoted window actually matches source code/diff text."""

    def __init__(self, max_drift: int = MAX_DRIFT, min_prefix: int = MIN_PREFIX) -> None:
        self.max_drift = max_drift
        self.min_prefix = min_prefix

    def parse_window_rows(
        self, window: object, default_line: int | None = None
    ) -> list[tuple[int, str]]:
        """Extract [(claimed_line, text)] from a finding's window string."""
        rows: list[tuple[int, str]] = []
        raw_lines = str(window or "").splitlines()
        for raw in raw_lines:
            match = WINDOW_LINE.match(raw)
            if match:
                rows.append(
                    (
                        int(match.group(1)),
                        re.sub(r"(\.{3}|\u2026)\s*$", "", match.group(2)),
                    )
                )

        if not rows and default_line is not None:
            for idx, raw in enumerate(raw_lines):
                clean = raw.strip()
                if clean:
                    rows.append(
                        (
                            default_line + idx,
                            re.sub(r"(\.{3}|\u2026)\s*$", "", clean),
                        )
                    )
        return rows

    def window_matches(
        self, rows: list[tuple[int, str]], lines: dict[int, str], offset: int
    ) -> int:
        """How many window rows match lines at this offset; 0 if any conflicts.

        A row whose line the diff does not show is not a conflict, but does not count.
        """
        matched = 0
        for number, claimed in rows:
            actual = lines.get(number + offset)
            if actual is None:
                continue
            want, have = norm(claimed), norm(actual)
            if not want:
                continue
            if want == have or (
                min(len(want), len(have)) >= self.min_prefix
                and (want.startswith(have) or have.startswith(want))
            ):
                matched += 1
            else:
                return 0
        return matched

    def snap_to_window(self, line: int, rows: list[tuple[int, str]], offset: int) -> int:
        """Pull an anchor that sits outside its own verified window back into it."""
        claimed = [number + offset for number, _text in rows]
        if line in claimed:
            return line
        return min(claimed, key=lambda candidate: (abs(candidate - line), candidate))

    def check_window(
        self,
        window: object,
        line: int,
        lines: dict[int, str],
    ) -> tuple[bool, int, str]:
        """(verified, line, reason) for a candidate finding's quoted window.

        Returns:
            (verified, corrected_line, reason)
        """
        rows = self.parse_window_rows(window, line)
        if not rows:
            return False, line, "no window supplied"

        if not lines:
            return True, line, "no line text available for verification"

        # Check exact alignment (offset 0)
        if self.window_matches(rows, lines, 0):
            snapped = self.snap_to_window(line, rows, 0)
            if snapped != line:
                return (
                    True,
                    snapped,
                    f"anchor {line} was outside its own window; moved to {snapped}",
                )
            return True, line, "window matches the diff"

        # Check drift offsets
        for step in range(1, self.max_drift + 1):
            for offset in (step, -step):
                if self.window_matches(rows, lines, offset) >= 1:
                    snapped = self.snap_to_window(line + offset, rows, offset)
                    return (
                        True,
                        snapped,
                        f"window matched {offset:+d} lines away; anchor corrected",
                    )

        # Mismatch
        number, claimed = rows[0]
        actual = lines.get(number, "")
        return (
            False,
            line,
            f"window says {claimed.strip()[:40]!r}, code has {actual.strip()[:40]!r}",
        )
