"""Optional live replay runner for review grounding checks.

Manual-only developer script. It is deliberately gated behind ``--live``, never imported
by tests, and never executed in CI.

Usage::

    uv run python dev/replay_review.py --live --pr 258
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from webhook_agent.review.grounding import (
    check_body_metadata_parity,
    check_test_count_claims,
    diff_text_from_patches,
    ngram_containment,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "reviews"


def replay_pr_fixture(pr_number: int) -> int:
    """Load PR fixture and run replay grounding checks."""
    fixture_path = FIXTURES_DIR / f"pr_{pr_number}.json"
    if not fixture_path.exists():
        sys.stderr.write(f"Error: fixture for PR #{pr_number} not found at {fixture_path}\n")
        return 1

    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    diff_text = diff_text_from_patches(data.get("patches", {}))
    pr_body = data.get("body", "")
    reviews = [
        r for r in data.get("reviews", []) if str(r.get("login") or "").startswith("hannibal")
    ]

    sys.stdout.write(f"=== Replaying PR #{pr_number} ({len(reviews)} bot reviews) ===\n")

    for idx, review in enumerate(reviews):
        body = review.get("body") or ""
        rid = review.get("id")
        sys.stdout.write(f"\n--- Review #{idx + 1} (ID: {rid}) ---\n")

        # Test count claims check
        test_issues = check_test_count_claims(body, diff_text)
        sys.stdout.write(f"Test-count issues: {len(test_issues)}\n")
        for issue in test_issues:
            sys.stdout.write(f"  [{issue.severity.upper()}] {issue.reason}\n")

        # Parity check
        match = (
            json.loads(body.split("<!-- hannibal-review-metadata: ")[1].split(" -->")[0])
            if "<!-- hannibal-review-metadata: " in body
            else {}
        )
        parity_issues = check_body_metadata_parity(body, match)
        sys.stdout.write(f"Body/metadata parity issues: {len(parity_issues)}\n")
        for issue in parity_issues:
            sys.stdout.write(f"  [{issue.severity.upper()}] {issue.reason}\n")

        # Narrative echo score
        echo_score = ngram_containment(pr_body, body, n=5)
        sys.stdout.write(f"Narrative echo (n=5 containment): {echo_score:.4f}\n")

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for dev/replay_review.py."""
    parser = argparse.ArgumentParser(description="Live review replay runner.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Mandatory flag gating live/manual replay execution.",
    )
    parser.add_argument("--pr", type=int, default=258, help="PR fixture number to replay.")
    args = parser.parse_args(argv)

    if not args.live:
        sys.stderr.write("Error: --live flag is required to run dev/replay_review.py\n")
        return 1

    return replay_pr_fixture(args.pr)


if __name__ == "__main__":
    sys.exit(main())
