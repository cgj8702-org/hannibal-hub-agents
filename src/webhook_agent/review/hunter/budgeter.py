"""Deterministic Review Budgeter and Churn Pruner for single-agent review planning.

Adapted from google/adk-samples/.agents/skills/github-pr-review/scripts/plan_review.py.
Computes reviewable churn (pruning lockfiles, binaries, generated files, vendored code)
and calculates a comment budget to prevent comment flooding while pairing source files with tests.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

# (pattern, reason) -- matched against full file path
SKIP_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|uv\.lock)$",
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
        r"\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|tar|gz|jar|so|dylib|dll)$",
        "binary asset",
    ),
    (r"(^|/)testdata/", "test fixture"),
    (r"(^|/)fixtures?/", "test fixture"),
)

BULK_DATA_EXT = frozenset(
    {
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
)
BULK_DATA_CHURN = 500

# Comment budget by reviewable churn: (max_churn, low, high)
BUDGET_TABLE: tuple[tuple[float, int, int], ...] = (
    (50, 2, 3),
    (200, 3, 5),
    (600, 5, 8),
    (1500, 8, 12),
    (math.inf, 12, 20),
)

TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/", re.IGNORECASE)


class ReviewBudgeter:
    """Computes reviewable churn, comment budget, and source-test pairings."""

    def __init__(
        self,
        skip_patterns: tuple[tuple[str, str], ...] = SKIP_PATTERNS,
        budget_table: tuple[tuple[float, int, int], ...] = BUDGET_TABLE,
    ) -> None:
        self.skip_patterns = skip_patterns
        self.budget_table = budget_table

    def skip_reason(self, path: str, churn: int = 0) -> str | None:
        """Return skip reason if file is excluded from reviewable code, or None."""
        clean_path = path[2:] if path.startswith(("a/", "b/")) else path
        for pattern, reason in self.skip_patterns:
            if re.search(pattern, clean_path, re.IGNORECASE):
                return reason

        ext = (
            clean_path[clean_path.rfind(".") :].lower()
            if "." in clean_path.rsplit("/", 1)[-1]
            else ""
        )
        if ext in BULK_DATA_EXT and churn >= BULK_DATA_CHURN:
            return "bulk data"

        return None

    def budget_for(self, reviewable_churn: int) -> tuple[int, int]:
        """(low, high) comment budget for given reviewable line churn."""
        for ceiling, low, high in self.budget_table:
            if reviewable_churn <= ceiling:
                return low, high
        return 12, 20

    def pair_sources_and_tests(self, files: list[str]) -> tuple[dict[str, list[str]], list[str]]:
        """Pair test files with their corresponding source files.

        Returns:
            (paired_map, unpaired_tests)
        """
        sources: set[str] = set()
        tests: set[str] = set()

        for f in files:
            clean = f[2:] if f.startswith(("a/", "b/")) else f
            if (
                TEST_DIR_RE.search(clean)
                or Path(clean).name.startswith("test_")
                or clean.endswith(("_test.py", "_spec.js"))
            ):
                tests.add(clean)
            else:
                sources.add(clean)

        pairings: dict[str, list[str]] = {s: [] for s in sources}
        unpaired: list[str] = []

        for t in tests:
            t_name = Path(t).name
            stem = t_name
            if stem.startswith("test_"):
                stem = stem[5:]
            if stem.endswith((".py", ".js", ".ts")):
                stem = stem[: stem.rfind(".")]
            if stem.endswith(("_test", "_spec")):
                stem = stem.rsplit("_", 1)[0]

            matched = False
            for s in sources:
                s_name = Path(s).stem
                if stem and (s_name == stem or s_name.endswith(f"_{stem}")):
                    pairings[s].append(t)
                    matched = True
                    break
            if not matched:
                unpaired.append(t)

        return pairings, unpaired

    def plan_review(self, changed_files: list[dict[str, Any]] | list[str]) -> dict[str, Any]:
        """Compute review plan including reviewable files, churn, and budget."""
        normalized_files: list[tuple[str, int]] = []
        for item in changed_files:
            if isinstance(item, dict):
                path = str(item.get("filename") or item.get("path") or "")
                churn = int(
                    item.get("changes")
                    or (item.get("additions", 0) + item.get("deletions", 0))
                    or 0
                )
                normalized_files.append((path, churn))
            else:
                normalized_files.append((str(item), 0))

        reviewable: list[str] = []
        skipped: dict[str, str] = {}
        total_churn = 0
        reviewable_churn = 0

        for path, churn in normalized_files:
            total_churn += churn
            reason = self.skip_reason(path, churn)
            if reason:
                skipped[path] = reason
            else:
                reviewable.append(path)
                reviewable_churn += churn

        # If churn wasn't available in inputs, fallback to count of reviewable files * 20 lines
        estimated_churn = reviewable_churn if reviewable_churn > 0 else len(reviewable) * 20
        low, high = self.budget_for(estimated_churn)
        pairings, unpaired_tests = self.pair_sources_and_tests(reviewable)

        return {
            "total_files": len(normalized_files),
            "reviewable_files": reviewable,
            "skipped_files": skipped,
            "total_churn": total_churn,
            "reviewable_churn": reviewable_churn,
            "budget": {
                "low": low,
                "high": high,
                "mid": (low + high) // 2,
            },
            "source_test_pairings": pairings,
            "unpaired_tests": unpaired_tests,
        }
