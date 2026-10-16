"""Base environment interface for sandboxed execution.

Lifts the BaseEnvironment pattern from Google's long-horizon-harness, providing
a consistent, safe abstraction for executing commands, reading files, and inspecting
directories within disposable or containerized workspaces.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from webhook_agent.environment.paths import path_under_root


@dataclass(frozen=True)
class ExecutionResult:
    """Result of a command executed in an environment."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Return True if command finished with exit code 0."""
        return self.exit_code == 0


class BaseEnvironment(abc.ABC):
    """Abstract interface for sandboxed execution environments."""

    def __init__(self, working_dir: Path) -> None:
        self._working_dir = working_dir.resolve()

    @property
    def working_dir(self) -> Path:
        """The root working directory of this environment."""
        return self._working_dir

    @property
    def root(self) -> Path:
        """Alias for working_dir."""
        return self._working_dir

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseEnvironment):
            return False
        return self._working_dir == other._working_dir

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(working_dir={self._working_dir!r})"

    def resolve_path(self, path: str | Path) -> Path | None:
        """Resolve a relative or absolute path ensuring it is under this working_dir."""
        return path_under_root(path, self._working_dir)

    @abc.abstractmethod
    async def execute(
        self,
        command: str,
        timeout: float = 30.0,  # noqa: ASYNC109
        env_vars: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Execute a shell command within the environment.

        Args:
            command: Shell command string to execute.
            timeout: Maximum execution timeout in seconds.
            env_vars: Optional environment variables to merge.

        Returns:
            ExecutionResult containing exit_code, stdout, and stderr.
        """

    @abc.abstractmethod
    async def read_file(
        self,
        path: str | Path,
        max_chars: int | None = None,
    ) -> str:
        """Read text contents of a file within the environment.

        Args:
            path: Path to the target file (must be contained within working_dir).
            max_chars: Optional character limit for reading.

        Returns:
            Decoded text content of the file.
        """

    @abc.abstractmethod
    async def write_file(
        self,
        path: str | Path,
        content: str,
    ) -> None:
        """Write text contents to a file within the environment.

        Args:
            path: Path to the target file (must be contained within working_dir).
            content: Text content to write.
        """

    @abc.abstractmethod
    async def list_dir(
        self,
        path: str | Path = ".",
    ) -> list[str]:
        """List entries in a directory within the environment."""

    def is_file(self, path: str | Path) -> bool:
        """Return True if path resolves under working_dir and is an existing file."""
        resolved = self.resolve_path(path)
        return resolved is not None and resolved.is_file()

    def is_dir(self, path: str | Path) -> bool:
        """Return True if path resolves under working_dir and is an existing directory."""
        resolved = self.resolve_path(path)
        return resolved is not None and resolved.is_dir()
