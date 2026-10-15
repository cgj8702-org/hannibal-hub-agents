"""Unit tests for sandboxed code search tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from webhook_agent.environment.local import LocalEnvironment
from webhook_agent.tools.search_tools import search_files

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_search_files_finds_matches():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            # Create files
            (root / "src").mkdir()
            (root / "src" / "alpha.py").write_text("def find_me_function():\n    return 42\n")
            (root / "src" / "beta.py").write_text("# unrelated comment\nx = 10\n")
            (root / "README.md").write_text("# Title\nfind_me_function is documented here.\n")

            # Search for pattern
            res = await search_files(pattern="find_me_function", env=env)
            assert "error" not in res
            assert res["total"] == 2
            paths = [m["path"] for m in res["matches"]]
            assert any("alpha.py" in p for p in paths)
            assert any("README.md" in p for p in paths)

    asyncio.run(_run())


def test_search_files_file_glob_and_limit():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            (root / "one.py").write_text("target_token = 1")
            (root / "two.py").write_text("target_token = 2")
            (root / "three.txt").write_text("target_token = 3")

            # Glob filter to *.py only
            res = await search_files(pattern="target_token", file_glob="*.py", env=env)
            assert "error" not in res
            assert res["total"] == 2
            assert all(m["path"].endswith(".py") for m in res["matches"])

            # Limit to 1 match
            limited = await search_files(pattern="target_token", limit=1, env=env)
            assert limited["total"] == 1

    asyncio.run(_run())


def test_search_files_skips_binary_and_ignored_dirs():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            # Ignored dir
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("needle_in_git = True")

            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "cached.pyc").write_text("needle_in_cache")

            # Binary file
            (root / "image.png").write_text("needle_in_png")

            # Regular file
            (root / "clean.py").write_text("needle_found_here = True")

            res = await search_files(pattern="needle_", env=env)
            assert res["total"] == 1
            assert "clean.py" in res["matches"][0]["path"]

    asyncio.run(_run())


def test_search_files_invalid_regex():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            res = await search_files(pattern="[unclosed-regex", env=env)
            assert "error" in res
            assert "Invalid search regex" in res["error"]

    asyncio.run(_run())


def test_search_files_path_traversal_denied():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            res = await search_files(pattern="pattern", path="../../outside", env=env)
            assert "error" in res
            assert "escapes environment boundary" in res["error"]

    asyncio.run(_run())
