"""Unit tests for sandboxed code search tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from google.adk.tools import FunctionTool

from webhook_agent.environment.context import use_environment
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
            with use_environment(env):
                res = await search_files(pattern="find_me_function")
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

            with use_environment(env):
                # Glob filter to *.py only
                res = await search_files(pattern="target_token", file_glob="*.py")
                assert "error" not in res
                assert res["total"] == 2
                assert all(m["path"].endswith(".py") for m in res["matches"])

                # Limit to 1 match
                limited = await search_files(pattern="target_token", limit=1)
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

            with use_environment(env):
                res = await search_files(pattern="needle_")
            assert res["total"] == 1
            assert "clean.py" in res["matches"][0]["path"]

    asyncio.run(_run())


def test_search_files_invalid_regex():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            with use_environment(env):
                res = await search_files(pattern="[unclosed-regex")
            assert "error" in res
            assert "Invalid search regex" in res["error"]

    asyncio.run(_run())


def test_search_files_path_traversal_denied():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)

            with use_environment(env):
                res = await search_files(pattern="pattern", path="../../outside")
            assert "error" in res
            assert "escapes environment boundary" in res["error"]

    asyncio.run(_run())


def test_search_files_function_tool_schema_declaration():
    """Verify that search_files generates a valid FunctionTool schema declaration without PydanticSchemaGenerationError."""
    tool = FunctionTool(search_files)
    decl = tool._get_declaration()
    assert decl is not None
    assert decl.name == "search_files"
    assert "Search text files" in (decl.description or "")


def test_search_files_blocks_symlink_escape():
    """Verify that search_files ignores symlinks pointing outside the workspace root."""

    async def _run():
        with tempfile.TemporaryDirectory() as parent_dir:
            parent = Path(parent_dir)
            outside_file = parent / "secret.txt"
            outside_file.write_text("SUPER_SECRET_TOKEN_OUTSIDE = 'leak'")

            workspace = parent / "workspace"
            workspace.mkdir()
            env = LocalEnvironment(workspace)

            # Create a symlink inside workspace pointing outside
            symlink = workspace / "leaked_symlink.txt"
            symlink.symlink_to(outside_file)

            # Also create an internal file
            internal_file = workspace / "normal.txt"
            internal_file.write_text("NORMAL_TOKEN = 'safe'")

            with use_environment(env):
                res = await search_files(pattern="SUPER_SECRET_TOKEN_OUTSIDE")
                assert "error" not in res
                assert res["total"] == 0
                assert res["matches"] == []

                res_safe = await search_files(pattern="NORMAL_TOKEN")
                assert res_safe["total"] == 1

    asyncio.run(_run())


def test_search_files_rejects_overlong_pattern():
    """Verify that patterns longer than 500 characters are rejected with an error dict."""

    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = LocalEnvironment(Path(tmp_dir))
            with use_environment(env):
                res = await search_files(pattern="a" * 501)
                assert "error" in res
                assert res["matches"] == []
                assert res["total"] == 0

    asyncio.run(_run())


def test_search_files_clamps_limit():
    """Verify that search_files clamps limit within [1, 200]."""

    async def _run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = LocalEnvironment(root)
            for i in range(5):
                (root / f"file_{i}.txt").write_text("common_token = True\n")

            with use_environment(env):
                # Negative or zero limit clamps to 1
                res_low = await search_files(pattern="common_token", limit=-10)
                assert res_low["total"] == 1

                # Normal limit works
                res_normal = await search_files(pattern="common_token", limit=3)
                assert res_normal["total"] == 3

    asyncio.run(_run())
