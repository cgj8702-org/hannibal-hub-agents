"""GitHub App credential helper

Usage (CLI):

    python github_app_credential_helper.py \
        --app-id 12345 \
        --installation-id 67890 \
        --private-key ./path/to/private-key.pem

Functions:
- generate_jwt(app_id, private_key_pem)
- get_installation_token(jwt, installation_id)
- cached token storage in user's cache dir

This is a minimal, secure-by-default skeleton for obtaining and caching
GitHub App installation tokens. Do NOT commit private keys to source control.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt


@dataclass
class InstallationToken:
    token: str
    expires_at: str


def generate_jwt(app_id: int, private_key_pem: str, expire_seconds: int = 600) -> str:
    """Generate a GitHub App JWT (RS256).

    app_id: numeric App ID
    private_key_pem: PEM contents (str)
    expire_seconds: token lifetime in seconds (max 600 recommended)
    """
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + expire_seconds, "iss": str(app_id)}
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    # pyjwt may return bytes in some versions
    if isinstance(token, bytes):
        token = token.decode()
    return token


def get_installation_token(
    jwt_token: str, installation_id: int, github_api: str = "https://api.github.com"
) -> InstallationToken:
    """Exchange App JWT for installation access token.

    Returns InstallationToken(token, expires_at)
    Raises httpx.HTTPError on network errors or non-2xx responses.
    """
    url = f"{github_api}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-app-credential-helper/1.0",
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return InstallationToken(token=data["token"], expires_at=data["expires_at"])


def cache_path_for_installation(installation_id: int) -> Path:
    cache_dir = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "github_app_helper"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"install_token_{installation_id}.json"


def load_cached_token(
    installation_id: int, min_ttl_seconds: int = 60
) -> InstallationToken | None:
    p = cache_path_for_installation(installation_id)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        expires_at = raw.get("expires_at")
        token = raw.get("token")
        if not token or not expires_at:
            return None
        # expires_at is ISO8601 (e.g. 2026-07-02T01:53:35Z); parse to timezone-aware timestamp
        from datetime import datetime

        clean_expires = expires_at.replace("Z", "+00:00")
        exp_epoch = int(datetime.fromisoformat(clean_expires).timestamp())
        if exp_epoch - int(time.time()) < min_ttl_seconds:
            return None
        return InstallationToken(token=token, expires_at=expires_at)
    except Exception:
        return None


def save_cached_token(installation_id: int, token: InstallationToken) -> None:
    p = cache_path_for_installation(installation_id)
    p.write_text(json.dumps({"token": token.token, "expires_at": token.expires_at}))


def load_private_key(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"private key not found: {path}")
    return p.read_text()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="GitHub App credential helper: get/cached installation token"
    )
    ap.add_argument("--app-id", required=True, type=int, help="GitHub App numeric ID")
    ap.add_argument(
        "--installation-id",
        required=True,
        type=int,
        help="Installation ID to request token for",
    )
    ap.add_argument(
        "--private-key", required=True, help="Path to GitHub App private key (PEM)"
    )
    ap.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cache and fetch a new token",
    )
    ap.add_argument(
        "--print-only-token", action="store_true", help="Print only the token string"
    )
    ap.add_argument(
        "--github-api", default="https://api.github.com", help="GitHub API base URL"
    )
    args = ap.parse_args()

    if not args.force_refresh:
        cached = load_cached_token(args.installation_id)
        if cached:
            if args.print_only_token:
                print(cached.token)
            else:
                print(
                    json.dumps({"token": cached.token, "expires_at": cached.expires_at})
                )
            return 0

    private_key_pem = load_private_key(args.private_key)
    jwt_token = generate_jwt(args.app_id, private_key_pem)
    try:
        inst_tok = get_installation_token(
            jwt_token, args.installation_id, github_api=args.github_api
        )
    except httpx.HTTPStatusError as exc:
        print(f"Error from GitHub API: {exc.response.status_code} {exc.response.text}")
        return 2
    except Exception as exc:  # network or other errors
        print(f"Network/error: {exc}")
        return 3

    save_cached_token(args.installation_id, inst_tok)
    if args.print_only_token:
        print(inst_tok.token)
    else:
        print(json.dumps({"token": inst_tok.token, "expires_at": inst_tok.expires_at}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
