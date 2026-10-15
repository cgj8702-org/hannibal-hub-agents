"""Path containment and resolution utilities for sandboxed environments.

Re-exports path containment helpers from webhook_agent.environment.paths.
"""

from webhook_agent.environment.paths import path_under_root, resolve_sandboxed_path

__all__ = ["path_under_root", "resolve_sandboxed_path"]
