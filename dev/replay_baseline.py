"""Replay recorded PR fixtures and render baseline grounding metrics.

Developer-run script to evaluate provenance, citation grounding, and
hallucination rates across golden PR review fixtures.

Usage::

    # Run over all fixtures in tests/fixtures/reviews/
    uv run python dev/replay_baseline.py

    # Filter to specific PR numbers
    uv run python dev/replay_baseline.py --pr 258 260

    # Output as GitHub markdown
    uv run python dev/replay_baseline.py --format markdown

    # Output as JSON
    uv run python dev/replay_baseline.py --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path for developer-run execution
_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from webhook_agent.review.replay import (
    evaluate_fixture_dir,
    evaluate_fixtures,
    format_baseline_markdown,
    format_baseline_table,
    load_fixture_file,
)

DEFAULT_FIXTURES_DIR = Path("tests/fixtures/reviews")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for replay baseline evaluator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help="Directory containing pr_<n>.json review fixtures.",
    )
    parser.add_argument(
        "--pr",
        type=int,
        nargs="+",
        help="Optional specific PR number(s) to evaluate.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "markdown", "json"),
        default="table",
        help="Output format (table, markdown, or json).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero code if any hard failures are detected.",
    )
    args = parser.parse_args(argv)

    fixtures_dir: Path = args.fixtures_dir
    if not fixtures_dir.exists():
        sys.stderr.write(f"Error: Fixtures directory not found: {fixtures_dir}\n")
        return 2

    if args.pr:
        fixtures: list[dict[str, Any]] = []
        for pr_num in args.pr:
            pr_path = fixtures_dir / f"pr_{pr_num}.json"
            if not pr_path.exists():
                sys.stderr.write(f"Error: Fixture for PR #{pr_num} not found at {pr_path}\n")
                return 2
            fixtures.append(load_fixture_file(pr_path))
        report = evaluate_fixtures(fixtures)
    else:
        report = evaluate_fixture_dir(fixtures_dir)

    if args.format == "table":
        sys.stdout.write(format_baseline_table(report) + "\n")
    elif args.format == "markdown":
        sys.stdout.write(format_baseline_markdown(report) + "\n")
    elif args.format == "json":
        data = {
            "total_prs": report.total_prs,
            "total_reviews": report.total_reviews,
            "total_claims": report.total_claims,
            "total_hard_issues": report.total_hard_issues,
            "total_soft_issues": report.total_soft_issues,
            "total_hard_resolutions": report.total_hard_resolutions,
            "overall_accuracy": round(report.overall_accuracy, 2),
            "hallucination_rate": round(report.hallucination_rate, 2),
            "hard_issues_by_code": report.hard_issues_by_code,
            "soft_issues_by_code": report.soft_issues_by_code,
            "fixtures": [
                {
                    "pr_number": f.pr_number,
                    "repo": f.repo,
                    "title": f.title,
                    "reviews_count": f.total_reviews,
                    "claims_count": f.total_claims,
                    "hard_issues_count": f.hard_issues_count,
                    "soft_issues_count": f.soft_issues_count,
                    "accuracy": round(f.grounding_accuracy, 2),
                    "is_clean": f.is_clean,
                }
                for f in report.fixtures
            ],
        }
        sys.stdout.write(json.dumps(data, indent=2) + "\n")

    if args.strict and report.total_hard_issues > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
