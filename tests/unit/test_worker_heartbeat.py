"""Unit tests for worker watchdog and heartbeat telemetry."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from webhook_agent.worker import (
    get_heartbeat_path,
    notify_systemd_watchdog,
    touch_worker_heartbeat,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_get_heartbeat_path_xdg_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify get_heartbeat_path prefers XDG_RUNTIME_DIR when present."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = get_heartbeat_path()
    assert path == tmp_path / "hannibal-webhook-agent.heartbeat"


def test_get_heartbeat_path_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_heartbeat_path falls back to /tmp when XDG_RUNTIME_DIR is absent."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = get_heartbeat_path()
    assert path == Path("/tmp/hannibal-webhook-agent.heartbeat")


def test_touch_worker_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify touch_worker_heartbeat creates/updates the heartbeat file with valid epoch timestamp."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    before = time.time()
    touch_worker_heartbeat()
    after = time.time()

    heartbeat_file = tmp_path / "hannibal-webhook-agent.heartbeat"
    assert heartbeat_file.exists()

    content = heartbeat_file.read_text(encoding="utf-8")
    ts = float(content)
    assert before - 1.0 <= ts <= after + 1.0


def test_notify_systemd_watchdog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify notify_systemd_watchdog sends WATCHDOG=1 datagram to NOTIFY_SOCKET."""
    sock_path = str(tmp_path / "notify.sock")
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server_sock.bind(sock_path)
    server_sock.settimeout(2.0)

    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        notify_systemd_watchdog()

        data, _ = server_sock.recvfrom(1024)
        assert data == b"WATCHDOG=1"
    finally:
        server_sock.close()
