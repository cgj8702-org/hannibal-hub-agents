"""Environment abstraction package for sandboxed workspace execution."""

from webhook_agent.environment.base import BaseEnvironment, ExecutionResult
from webhook_agent.environment.context import (
    active_environment,
    ephemeral_environment,
    reset_active_environment,
    set_active_environment,
    use_environment,
)
from webhook_agent.environment.local import LocalEnvironment
from webhook_agent.environment.paths import path_under_root, resolve_sandboxed_path

__all__ = [
    "BaseEnvironment",
    "ExecutionResult",
    "LocalEnvironment",
    "active_environment",
    "ephemeral_environment",
    "path_under_root",
    "reset_active_environment",
    "resolve_sandboxed_path",
    "set_active_environment",
    "use_environment",
]
