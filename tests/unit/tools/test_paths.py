"""Unit tests for path containment and sandboxed path resolution."""

import os
import tempfile
from pathlib import Path

import pytest

from webhook_agent.environment.context import use_environment
from webhook_agent.environment.local import LocalEnvironment
from webhook_agent.environment.paths import path_under_root, resolve_sandboxed_path

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_path_under_root_valid_contained():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        sub_dir = root / "src" / "pkg"
        sub_dir.mkdir(parents=True)
        target_file = sub_dir / "module.py"
        target_file.write_text("x = 1")

        # Relative path
        resolved = path_under_root("src/pkg/module.py", root)
        assert resolved is not None
        assert resolved == target_file.resolve()

        # Absolute path under root
        resolved_abs = path_under_root(str(target_file), root)
        assert resolved_abs is not None
        assert resolved_abs == target_file.resolve()


def test_path_under_root_blocks_traversal():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir) / "workspace"
        root.mkdir()

        # Path traversal with ..
        assert path_under_root("../secret.txt", root) is None
        assert path_under_root("../../etc/passwd", root) is None
        assert path_under_root("sub/../../escaped.txt", root) is None

        # Absolute path pointing outside root
        outside_file = Path(tmp_dir) / "outside.txt"
        outside_file.write_text("secret")
        assert path_under_root(str(outside_file), root) is None


def test_path_under_root_blocks_symlink_escape():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir) / "workspace"
        root.mkdir()
        outside = Path(tmp_dir) / "outside_dir"
        outside.mkdir()
        target = outside / "secret.py"
        target.write_text("secret = True")

        symlink = root / "leak_link"
        try:
            symlink.symlink_to(outside)
        except OSError:
            pytest.skip("Symlinks not supported in this test environment")

        # Attempting to read target via symlink inside root
        assert path_under_root("leak_link/secret.py", root) is None


def test_resolve_sandboxed_path_with_environment():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        test_file = root / "app.py"
        test_file.write_text("print('hello')")

        env = LocalEnvironment(root)
        with use_environment(env):
            resolved = resolve_sandboxed_path("app.py")
            assert resolved is not None
            assert resolved == test_file.resolve()

            # Denied outside path
            assert resolve_sandboxed_path("../outside.py") is None
