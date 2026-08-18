"""ContextVar Environment interface for feature_agent.

Abstract Environment contract (LocalEnvironment and SandboxEnvironment) backed
by Python ContextVar for polymorphic workspace execution and auth refreshing.
"""

from __future__ import annotations

import abc
import contextvars
import logging
from pathlib import Path

logger = logging.getLogger("feature_agent.environment")


class Environment(abc.ABC):
    """Abstract Environment runtime contract for feature engineering tools."""

    @property
    @abc.abstractmethod
    def on_host_fs(self) -> bool:
        """Returns True if tools execute directly on host filesystem."""
        pass

    @abc.abstractmethod
    def refresh_auth(self) -> bool:
        """Refresh environment credentials. Returns False if environment is gone."""
        pass

    @abc.abstractmethod
    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a relative path inside the environment workspace."""
        pass


class LocalEnvironment(Environment):
    """Local workspace environment implementation."""

    def __init__(self, workspace_root: str | Path = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()

    @property
    def on_host_fs(self) -> bool:
        return True

    def refresh_auth(self) -> bool:
        return True

    def resolve_path(self, relative_path: str) -> Path:
        resolved = (self.workspace_root / relative_path).resolve()
        if not str(resolved).startswith(str(self.workspace_root)):
            raise PermissionError(
                f"Path traversal blocked: '{relative_path}' is outside workspace '{self.workspace_root}'"
            )
        return resolved


_active_env: contextvars.ContextVar[Environment | None] = contextvars.ContextVar(
    "_active_env", default=None
)


def active_environment() -> Environment:
    """Retrieve currently active Environment, defaulting to LocalEnvironment."""
    env = _active_env.get()
    if env is None:
        return LocalEnvironment()
    return env


def set_active_environment(env: Environment) -> contextvars.Token[Environment | None]:
    """Set the active Environment in ContextVar."""
    return _active_env.set(env)
