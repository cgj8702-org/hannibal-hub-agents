"""Local filesystem implementation of BaseEnvironment.

Provides process execution and file operations confined strictly to a designated
workspace root directory on the local host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from webhook_agent.environment.base import BaseEnvironment, ExecutionResult

logger = logging.getLogger(__name__)

_ALLOWED_SUBPROCESS_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONIOENCODING",
)


class LocalEnvironment(BaseEnvironment):
    """Local execution environment bounded to a specific directory."""

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,  # noqa: ASYNC109
        env_vars: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Execute a command via subprocess inside this environment's working directory."""
        env = {k: os.environ[k] for k in _ALLOWED_SUBPROCESS_ENV_VARS if k in os.environ}
        if env_vars:
            env.update(env_vars)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self._working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except TimeoutError:
                try:
                    if proc.pid is not None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except ProcessLookupError:
                    pass
                except Exception as kill_err:
                    logger.debug("Failed killing timed-out process group: %s", kill_err)
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                )

            return ExecutionResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except Exception as exc:
            logger.error("Error executing command '%s' in %s: %s", command, self._working_dir, exc)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
            )

    async def read_file(
        self,
        path: str | Path,
        max_chars: int | None = None,
    ) -> str:
        """Read a file strictly within the environment's working directory."""
        resolved = self.resolve_path(path)
        if resolved is None:
            raise PermissionError(f"Access denied: path '{path}' escapes workspace root")

        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: '{path}'")

        text = resolved.read_text(encoding="utf-8", errors="replace")
        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars]
        return text

    async def write_file(
        self,
        path: str | Path,
        content: str,
    ) -> None:
        """Write content to a file strictly within the environment's working directory."""
        resolved = self.resolve_path(path)
        if resolved is None:
            raise PermissionError(f"Access denied: path '{path}' escapes workspace root")

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    async def list_dir(
        self,
        path: str | Path = ".",
    ) -> list[str]:
        """List directory contents strictly within the environment's working directory."""
        resolved = self.resolve_path(path)
        if resolved is None:
            raise PermissionError(f"Access denied: path '{path}' escapes workspace root")

        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: '{path}'")

        return sorted(os.listdir(resolved))
