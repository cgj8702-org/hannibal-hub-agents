"""Unit tests for BaseEnvironment and LocalEnvironment implementations."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from webhook_agent.environment.context import (
    active_environment,
    ephemeral_environment,
    use_environment,
)
from webhook_agent.environment.local import LocalEnvironment

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_local_environment_file_operations():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            # Write and read file
            await env.write_file("sub/hello.txt", "Hello Sandbox World!")
            assert env.is_file("sub/hello.txt")
            assert env.is_dir("sub")

            content = await env.read_file("sub/hello.txt")
            assert content == "Hello Sandbox World!"

            # Read with character cap
            capped = await env.read_file("sub/hello.txt", max_chars=5)
            assert capped == "Hello"

            # List dir
            listing = await env.list_dir("sub")
            assert listing == ["hello.txt"]

    asyncio.run(_run())


def test_local_environment_blocks_escapes():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            with pytest.raises(PermissionError):
                await env.read_file("../escape.txt")

            with pytest.raises(PermissionError):
                await env.write_file("../../escape.txt", "data")

            with pytest.raises(PermissionError):
                await env.list_dir("../..")

    asyncio.run(_run())


def test_local_environment_execute():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            # Basic command
            result = await env.execute("python3 -c 'print(\"SANDBOX_OK\")'")
            assert result.ok is True
            assert result.exit_code == 0
            assert "SANDBOX_OK" in result.stdout

            # Command failure
            err_res = await env.execute("python3 -c 'import sys; sys.exit(42)'")
            assert err_res.ok is False
            assert err_res.exit_code == 42

    asyncio.run(_run())


def test_local_environment_timeout():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            # Fast timeout
            result = await env.execute("python3 -c 'import time; time.sleep(2)'", timeout=0.1)
            assert result.ok is False
            assert result.exit_code == -1
            assert "timed out" in result.stderr.lower()

    asyncio.run(_run())


def test_ephemeral_environment_lifecycle():
    async def _run():
        initial_env = active_environment()
        created_path: Path | None = None

        async with ephemeral_environment() as env:
            created_path = env.working_dir
            assert created_path.is_dir()
            assert active_environment() == env

            await env.write_file("ephemeral.txt", "temp data")
            assert env.is_file("ephemeral.txt")

        # After exit, active environment reverts and temp dir is cleaned up
        assert active_environment() == initial_env
        assert created_path is not None
        assert not created_path.exists()

    asyncio.run(_run())
