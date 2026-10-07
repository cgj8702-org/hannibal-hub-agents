"""Unit tests for GitHub App credentials helper (webhook_agent.github.credentials)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from webhook_agent.github.credentials import (
    InstallationToken,
    cache_path_for_installation,
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


def test_installation_token_dataclass() -> None:
    token = InstallationToken(token="ghs_test123", expires_at="2026-10-07T12:00:00Z")
    assert token.token == "ghs_test123"
    assert token.expires_at == "2026-10-07T12:00:00Z"


def test_generate_jwt(rsa_private_key_pem: str) -> None:
    app_id = 123456
    token_str = generate_jwt(app_id, rsa_private_key_pem, expire_seconds=300)
    assert isinstance(token_str, str)

    # Decode without verifying signature to check payload claims
    decoded = jwt.decode(token_str, options={"verify_signature": False})
    assert decoded["iss"] == str(app_id)
    assert "iat" in decoded
    assert "exp" in decoded
    assert decoded["exp"] > decoded["iat"]


def test_load_private_key(tmp_path: Path) -> None:
    key_file = tmp_path / "test_key.pem"
    content = (
        "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    )
    key_file.write_text(content)

    loaded = load_private_key(str(key_file))
    assert loaded == content

    with pytest.raises(FileNotFoundError):
        load_private_key(str(tmp_path / "nonexistent.pem"))


def test_cache_path_for_installation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = cache_path_for_installation(42)
    assert path == tmp_path / "github_app_helper" / "install_token_42.json"
    assert path.parent.exists()


def test_save_and_load_cached_token_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Future timestamp in UTC ISO8601
    future_time = datetime.now(UTC).timestamp() + 3600
    future_iso = datetime.fromtimestamp(future_time, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    token = InstallationToken(token="ghs_cached_valid", expires_at=future_iso)
    save_cached_token(100, token)

    loaded = load_cached_token(100, min_ttl_seconds=60)
    assert loaded is not None
    assert loaded.token == "ghs_cached_valid"
    assert loaded.expires_at == future_iso


def test_load_cached_token_expired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Past timestamp
    past_iso = "2020-01-01T00:00:00Z"
    token = InstallationToken(token="ghs_expired", expires_at=past_iso)
    save_cached_token(101, token)

    loaded = load_cached_token(101, min_ttl_seconds=60)
    assert loaded is None


def test_load_cached_token_missing_or_corrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Missing file
    assert load_cached_token(999) is None

    # Corrupt JSON
    path = cache_path_for_installation(999)
    path.write_text("invalid json")
    assert load_cached_token(999) is None

    # Incomplete JSON
    path.write_text(json.dumps({"token": "ghs_no_exp"}))
    assert load_cached_token(999) is None


def test_get_installation_token_success() -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "token": "ghs_api_token",
        "expires_at": "2026-10-07T12:00:00Z",
    }
    mock_response.raise_for_status.return_value = None

    with patch("httpx.Client.post", return_value=mock_response):
        result = get_installation_token("jwt_xyz", 12345)
        assert result.token == "ghs_api_token"
        assert result.expires_at == "2026-10-07T12:00:00Z"


def test_get_installation_token_error() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=MagicMock()
    )

    with patch("httpx.Client.post", return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            get_installation_token("jwt_xyz", 12345)
