"""Record review fixtures for the offline replay harness.

Network-only, developer-run helper. It is deliberately *not* a pytest module and lives
under ``dev/`` (excluded from pytest collection via ``norecursedirs``) so the offline test
suite never touches the network.

Usage::

    python3 dev/record_review_fixtures.py 258 --patches full
    python3 dev/record_review_fixtures.py 241 --patches none

Output: ``tests/fixtures/reviews/pr_<n>.json``. Record once, commit, then iterate offline
forever. Re-recording is how you extend the golden set, not part of the normal test loop.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_REPO = "cgj8702-org/hannibal-hub-agents"
OUT_DIR = Path("tests/fixtures/reviews")


def gh_api(endpoint: str, *, paginate: bool = False) -> Any:
    """Call the GitHub API through the authenticated ``gh`` CLI."""
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {endpoint} failed: {proc.stderr.strip()}")
    text = proc.stdout.strip()
    if paginate:
        # --paginate concatenates JSON arrays (and/or newline-delimited objects).
        return _parse_paginated(text)
    return json.loads(text)


def _parse_paginated(text: str) -> list[Any]:
    """Flatten concatenated JSON arrays emitted by ``gh api --paginate``."""
    if not text:
        return []
    items: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1
        if idx >= len(text):
            break
        value, end = decoder.raw_decode(text, idx)
        items.extend(value if isinstance(value, list) else [value])
        idx = end
    return items


def record(pr_number: int, repo: str, patches: str) -> Path:
    """Record one pull request's metadata, changed files, and bot reviews."""
    pr = gh_api(f"repos/{repo}/pulls/{pr_number}")
    files = gh_api(f"repos/{repo}/pulls/{pr_number}/files", paginate=True)
    reviews = gh_api(f"repos/{repo}/pulls/{pr_number}/reviews", paginate=True)

    changed_files = sorted(str(f.get("filename")) for f in files if f.get("filename"))
    patch_map: dict[str, str | None] = {}
    if patches == "full":
        for entry in files:
            name = entry.get("filename")
            if name:
                patch_map[str(name)] = entry.get("patch")

    payload: dict[str, Any] = {
        "_note": (
            "Recorded fixture for the offline replay harness. Regenerate with "
            f"`python3 dev/record_review_fixtures.py {pr_number} --patches {patches}`. "
            "Do not hand-edit review bodies: they are evidence."
        ),
        "_patch_mode": patches,
        "repo": repo,
        "number": pr_number,
        "title": pr.get("title"),
        "body": pr.get("body"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": changed_files,
        "patches": patch_map,
        "reviews": [
            {
                "id": r.get("id"),
                "login": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "submitted_at": r.get("submitted_at"),
                "body": r.get("body"),
            }
            for r in reviews
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"pr_{pr_number}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr_number", type=int, help="Pull request number to record.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to record from.")
    parser.add_argument(
        "--patches",
        choices=("full", "none"),
        default="full",
        help="'full' stores per-file diff patches; 'none' stores changed paths only.",
    )
    args = parser.parse_args(argv)

    path = record(args.pr_number, args.repo, args.patches)
    size_kb = path.stat().st_size / 1024
    sys.stdout.write(f"wrote {path} ({size_kb:.1f} KiB)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
