"""Path containment and resolution utilities for sandboxed environments.

Ensures all file operations remain strictly contained within designated workspace
roots, barring any path traversal ('..') or symlink escape attacks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def path_under_root(path: str | Path, root: str | Path) -> Path | None:
    """Safely resolve `path` relative to `root` ensuring it stays under `root`.

    Args:
        path: Relative or absolute path candidate.
        root: The root boundary directory.

    Returns:
        The resolved Path if it is strictly contained within root, or None if
        it attempts to escape root (e.g. via '..' or symlinks).
    """
    try:
        root_path = Path(root).resolve()
        candidate = Path(path)

        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (root_path / candidate).resolve()

        # Ensure the resolved path starts with the resolved root path
        try:
            resolved.relative_to(root_path)
            return resolved
        except ValueError:
            logger.warning(
                "Access violation: path '%s' resolves outside root '%s'",
                path,
                root_path,
            )
            return None
    except Exception as exc:
        logger.debug("Error checking path containment for '%s': %s", path, exc)
        return None


def resolve_sandboxed_path(
    file_path: str | Path,
    root: str | Path | None = None,
) -> Path | None:
    """Resolve a file path candidate within the active environment or given root.

    Args:
        file_path: Path string or Path object to resolve.
        root: Optional root directory override. If omitted, attempts to use
              the active environment's working directory or the current working directory.

    Returns:
        A resolved Path strictly under the designated root, or None if invalid/outside.
    """
    if root is None:
        try:
            from webhook_agent.environment.context import active_environment

            env = active_environment()
            root = env.working_dir
        except Exception:
            root = Path(os.getcwd())

    return path_under_root(file_path, root)
