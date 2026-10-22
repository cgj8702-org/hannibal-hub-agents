"""ContextVar management and lifecycle for active execution environments."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar, Token
from pathlib import Path

from webhook_agent.environment.base import BaseEnvironment
from webhook_agent.environment.local import LocalEnvironment

_ACTIVE_ENV: ContextVar[BaseEnvironment | None] = ContextVar("active_environment", default=None)
_DEFAULT_ENV: BaseEnvironment | None = None


def active_environment() -> BaseEnvironment:
    """Return the currently active execution environment, or a default LocalEnvironment."""
    env = _ACTIVE_ENV.get()
    if env is not None:
        return env

    global _DEFAULT_ENV
    if _DEFAULT_ENV is None:
        _DEFAULT_ENV = LocalEnvironment(Path(os.getcwd()))
    return _DEFAULT_ENV


def get_active_environment() -> BaseEnvironment | None:
    """Return the currently set active execution environment, or None if none is set."""
    return _ACTIVE_ENV.get()


def set_active_environment(env: BaseEnvironment) -> Token[BaseEnvironment | None]:
    """Set the active execution environment for the current task/context."""
    return _ACTIVE_ENV.set(env)


def reset_active_environment(token: Token[BaseEnvironment | None]) -> None:
    """Reset the active execution environment back to the previous token."""
    _ACTIVE_ENV.reset(token)


@contextlib.contextmanager
def use_environment(env: BaseEnvironment) -> Iterator[BaseEnvironment]:
    """Context manager to scope an environment to a block of code."""
    token = set_active_environment(env)
    try:
        yield env
    finally:
        reset_active_environment(token)


@contextlib.asynccontextmanager
async def ephemeral_environment(
    root: Path | None = None,
    cleanup: bool = True,
    prefix: str = "hannibal-sandbox-",
) -> AsyncIterator[BaseEnvironment]:
    """Create and activate an ephemeral workspace environment for the duration of a block.

    Args:
        root: Optional existing root path. If None, creates a disposable temp directory.
        cleanup: Whether to delete the temporary directory upon exit (if created).
        prefix: Prefix for the temporary directory if root is None.
    """
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix=prefix)
        target_root = Path(temp_dir_obj.name)
    else:
        target_root = Path(root)

    env = LocalEnvironment(target_root)
    token = set_active_environment(env)
    try:
        yield env
    finally:
        reset_active_environment(token)
        if temp_dir_obj is not None and cleanup:
            with contextlib.suppress(Exception):
                temp_dir_obj.cleanup()
