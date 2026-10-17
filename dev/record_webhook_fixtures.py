"""Record real GitHub webhook deliveries as offline test fixtures.

Network-only, developer-run helper. It is deliberately *not* a pytest module and lives
under ``dev/`` (excluded from pytest collection via ``norecursedirs``) so the offline test
suite never touches the network.

The App deliveries endpoint requires **App-level** auth: it rejects installation tokens,
GitHub App user tokens, and fine-grained PATs. So this mints a JWT from the App private key
via ``webhook_agent.github.credentials.generate_jwt``.

Requires ``GITHUB_APP_ID`` and ``GITHUB_PRIVATE_KEY_PATH`` in the environment.

Usage::

    # See which event.action pairs are still available
    uv run python dev/record_webhook_fixtures.py --list

    # Record one fixture per requested pair
    uv run python dev/record_webhook_fixtures.py pull_request.opened issue_comment.created

Output: ``tests/fixtures/webhooks/<event>.<action>.json`` — the raw webhook payload body as
GitHub delivered it. Record once, commit, then iterate offline forever.

Only the payload body is recorded, never the request headers: the filename already encodes
the event and action, and the worker's input envelope carries ``event_name`` and
``delivery_id`` as flat strings, so no header data is needed by any test.

The deliveries API returns the payload as *parsed JSON*, so the original signed bytes are
not recoverable. These fixtures are for routing, normalization, and suppression tests —
**not** for signature verification. Build synthetic bodies for that.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# Ensure src/ is on sys.path for developer-run execution
_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from webhook_agent.github.credentials import generate_jwt, load_private_key

API_ROOT = "https://api.github.com"
OUT_DIR = Path("tests/fixtures/webhooks")
DEFAULT_PAGES = 5


def _require_env(name: str) -> str:
    """Return a required environment variable, or exit with a readable message."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set. Export it (or source .envrc) before running.")
    return value


def _client() -> httpx.Client:
    """Build an App-authenticated client. One JWT lasts 10 minutes, ample for a run."""
    app_id = int(_require_env("GITHUB_APP_ID"))
    private_key = load_private_key(_require_env("GITHUB_PRIVATE_KEY_PATH"))
    return httpx.Client(
        base_url=API_ROOT,
        headers={
            "Authorization": f"Bearer {generate_jwt(app_id, private_key)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )


def list_deliveries(client: httpx.Client, pages: int) -> list[dict[str, Any]]:
    """Page through the App's webhook deliveries. Each list item carries event + action."""
    deliveries: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        response = client.get("/app/hook/deliveries", params={"per_page": 100, "page": page})
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        deliveries.extend(batch)
    return deliveries


def pair_of(delivery: dict[str, Any]) -> str:
    """Return the ``<event>.<action>`` label for a delivery list item."""
    action = delivery.get("action")
    return f"{delivery['event']}.{action}" if action else str(delivery["event"])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the webhook fixture recorder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wanted", nargs="*", help="event.action pairs, e.g. pull_request.opened")
    parser.add_argument("--list", action="store_true", help="print available pairs and exit")
    parser.add_argument(
        "--pages", type=int, default=DEFAULT_PAGES, help="pages of 100 deliveries to scan"
    )
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with _client() as client:
        deliveries = list_deliveries(client, args.pages)

        if args.list or not args.wanted:
            print(f"{len(deliveries)} deliveries available:")
            for pair in sorted({pair_of(d) for d in deliveries}):
                print(f"  {pair}")
            return 0

        exit_code = 0
        for want in args.wanted:
            # First match wins. Re-run with a narrower --pages to reach an older delivery.
            match = next((d for d in deliveries if pair_of(d) == want), None)
            if match is None:
                print(f"!! no delivery found for {want}", file=sys.stderr)
                exit_code = 1
                continue

            detail = client.get(f"/app/hook/deliveries/{match['id']}")
            detail.raise_for_status()
            payload = detail.json()["request"]["payload"]

            target = OUT_DIR / f"{want}.json"
            target.write_text(json.dumps(payload, indent=2) + "\n")
            print(f"wrote {target}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
