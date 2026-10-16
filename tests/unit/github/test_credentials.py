"""Unit tests for GitHub App credentials helper (webhook_agent.github.credentials)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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


def test_cache_permissions_with_umask(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Ensure umask is 022 (standard default in many environments)
    old_umask = os.umask(0o022)
    try:
        token = InstallationToken(token="ghs_test_perms", expires_at="2030-01-01T00:00:00Z")
        save_cached_token(200, token)

        target_file = cache_path_for_installation(200)
        cache_dir = target_file.parent

        dir_mode = cache_dir.stat().st_mode & 0o777
        file_mode = target_file.stat().st_mode & 0o777

        assert dir_mode == 0o700, f"Cache dir permissions {oct(dir_mode)} != 0o700"
        assert file_mode == 0o600, f"Cache file permissions {oct(file_mode)} != 0o600"
    finally:
        os.umask(old_umask)


def test_cache_directory_chmod_if_already_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache_dir = tmp_path / "github_app_helper"
    cache_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o755)
    assert cache_dir.stat().st_mode & 0o777 == 0o755

    # Calling cache_path_for_installation should ensure 0700 mode
    cache_path_for_installation(201)
    assert cache_dir.stat().st_mode & 0o777 == 0o700


def test_save_cached_token_atomic_no_partial_on_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    target_file = cache_path_for_installation(202)
    cache_dir = target_file.parent

    token = InstallationToken(token="ghs_test_fail", expires_at="2030-01-01T00:00:00Z")

    # Simulate error during write
    with patch("os.replace", side_effect=OSError("Disk full")):
        with pytest.raises(OSError, match="Disk full"):
            save_cached_token(202, token)

    # Target file must not exist
    assert not target_file.exists()
    # No dangling .tmp files in cache directory
    temp_files = list(cache_dir.glob("install_token_202.json.tmp*"))
    assert temp_files == []


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
