"""Unit tests for pr_workspace context manager."""

import asyncio
import io
import logging
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.environment.context import active_environment, get_active_environment
from webhook_agent.environment.pr_workspace import (
    MAX_EXTRACTED_BYTES,
    MAX_FILES,
    pr_workspace,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def _create_test_tarball(
    files: dict[str, bytes],
    top_dir: str = "owner-repo-1234567",
    include_symlink: bool = False,
    include_hardlink: bool = False,
) -> bytes:
    """Create in-memory gzipped tarball with specified files under a top-level directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel_path, content in files.items():
            ti = tarfile.TarInfo(f"{top_dir}/{rel_path}")
            ti.size = len(content)
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(content))

        if include_symlink:
            sym = tarfile.TarInfo(f"{top_dir}/escape_symlink")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tar.addfile(sym)

        if include_hardlink:
            hl = tarfile.TarInfo(f"{top_dir}/escape_hardlink")
            hl.type = tarfile.LNKTYPE
            hl.linkname = f"{top_dir}/README.md"
            tar.addfile(hl)

    return buf.getvalue()


class MockStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self.chunks = chunks
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class MockAsyncClient:
    def __init__(self, stream_resp: MockStreamResponse):
        self.stream_resp = stream_resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def stream(self, method: str, url: str):
        return self.stream_resp


def test_pr_workspace_happy_path():
    """Happy path: extracts PR head tarball, strips root dir, confines env, and cleans up."""

    async def _run():
        tar_bytes = _create_test_tarball(
            {
                "src/app.py": b"print('Hello PR')",
                "README.md": b"# Welcome",
            }
        )

        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_repo.get_archive_link.return_value = "https://codeload.github.com/archive.tar.gz"

        client = MockAsyncClient(MockStreamResponse([tar_bytes]))

        extracted_dir: Path | None = None
        with patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is not None
                extracted_dir = env.root
                assert extracted_dir.is_dir()
                assert (extracted_dir / "src/app.py").is_file()
                assert (extracted_dir / "src/app.py").read_text() == "print('Hello PR')"
                assert (extracted_dir / "README.md").is_file()
                # Verify active environment is set to this workspace
                assert get_active_environment() == env
                assert active_environment() == env

        # After context exit, the directory must be cleaned up and active env reset
        assert extracted_dir is not None
        assert not extracted_dir.exists()
        assert get_active_environment() is None

    asyncio.run(_run())


def test_pr_workspace_rejects_symlinks_and_hardlinks():
    """Symlink and hardlink members in tarball are skipped/rejected during extraction."""

    async def _run():
        tar_bytes = _create_test_tarball(
            {"src/app.py": b"print('Safe')", "README.md": b"# Doc"},
            include_symlink=True,
            include_hardlink=True,
        )

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value.get_archive_link.return_value = "https://archive.tar.gz"

        client = MockAsyncClient(MockStreamResponse([tar_bytes]))

        with patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is not None
                assert (env.root / "src/app.py").is_file()
                assert not (env.root / "escape_symlink").exists()
                assert not (env.root / "escape_hardlink").exists()

    asyncio.run(_run())


def test_pr_workspace_rejects_traversal_and_absolute_paths():
    """Traversal members (../..) and absolute path members are skipped during extraction."""

    async def _run():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Valid file
            t1 = tarfile.TarInfo("root/safe.txt")
            t1.size = 4
            tar.addfile(t1, io.BytesIO(b"safe"))
            # Traversal member
            t2 = tarfile.TarInfo("root/../../escape_traversal.txt")
            t2.size = 3
            tar.addfile(t2, io.BytesIO(b"bad"))
            # Absolute member
            t3 = tarfile.TarInfo("/tmp/escape_abs.txt")
            t3.size = 3
            tar.addfile(t3, io.BytesIO(b"bad"))
        tar_bytes = buf.getvalue()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value.get_archive_link.return_value = "https://archive.tar.gz"

        client = MockAsyncClient(MockStreamResponse([tar_bytes]))

        with patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is not None
                assert (env.root / "safe.txt").is_file()
                assert (env.root / "safe.txt").read_text() == "safe"
                assert not (env.root / "escape_traversal.txt").exists()

    asyncio.run(_run())


def test_pr_workspace_kill_switch(monkeypatch):
    """When ENABLE_PR_WORKSPACE=0, yields None without network calls."""

    async def _run():
        monkeypatch.setenv("ENABLE_PR_WORKSPACE", "0")
        mock_gh = MagicMock()

        async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
            assert env is None

        mock_gh.get_repo.assert_not_called()

    asyncio.run(_run())


def test_pr_workspace_download_cap_exceeded(caplog):
    """Archive stream exceeding 100MB download cap aborts and yields None."""

    async def _run():
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value.get_archive_link.return_value = "https://archive.tar.gz"

        chunk_size = 50 * 1024 * 1024
        chunks = [b"A" * chunk_size, b"B" * chunk_size, b"C" * 1024]

        client = MockAsyncClient(MockStreamResponse(chunks))

        with caplog.at_level(logging.WARNING), patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is None

        assert "exceeded 100 MB download cap" in caplog.text

    asyncio.run(_run())


def test_pr_workspace_extracted_files_cap_exceeded(caplog):
    """Archive with more than 20,000 files aborts extraction and yields None."""

    async def _run():
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value.get_archive_link.return_value = "https://archive.tar.gz"

        # Create tarball with 20,005 dummy files
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for i in range(MAX_FILES + 5):
                ti = tarfile.TarInfo(f"root/f_{i}.txt")
                ti.size = 1
                ti.type = tarfile.REGTYPE
                tar.addfile(ti, io.BytesIO(b"x"))
        tar_bytes = buf.getvalue()

        client = MockAsyncClient(MockStreamResponse([tar_bytes]))

        with caplog.at_level(logging.WARNING), patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is None

        assert "Archive exceeded max file count limit" in caplog.text

    asyncio.run(_run())


def test_pr_workspace_extracted_bytes_cap_exceeded(caplog):
    """Archive declaring more than 200MB uncompressed size aborts and yields None."""

    async def _run():
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value.get_archive_link.return_value = "https://archive.tar.gz"

        class ZeroReader(io.RawIOBase):
            def __init__(self, size: int):
                self.remaining = size

            def readable(self) -> bool:
                return True

            def readinto(self, b) -> int:
                if self.remaining <= 0:
                    return 0
                n = min(len(b), self.remaining)
                b[:n] = b"\x00" * n
                self.remaining -= n
                return n

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo("root/huge.bin")
            ti.size = MAX_EXTRACTED_BYTES + 1024
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BufferedReader(ZeroReader(ti.size)))
        tar_bytes = buf.getvalue()

        client = MockAsyncClient(MockStreamResponse([tar_bytes]))

        with caplog.at_level(logging.WARNING), patch("httpx.AsyncClient", return_value=client):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is None

        assert "Archive exceeded max extracted size limit" in caplog.text

    asyncio.run(_run())


def test_pr_workspace_network_error_graceful_fallback(caplog):
    """When GitHub archive link retrieval fails, logs warning and yields None."""

    async def _run():
        mock_gh = MagicMock()
        mock_gh.get_repo.side_effect = Exception("GitHub API 500 Internal Error")

        with caplog.at_level(logging.WARNING):
            async with pr_workspace(mock_gh, "owner/repo", "headsha123") as env:
                assert env is None

        assert "Could not obtain archive download link" in caplog.text

    asyncio.run(_run())
