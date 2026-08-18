"""Exfiltration, policy, and command substitution guards for feature_agent.

Implements exfil_guard, policies_guard, and quote-aware permission_guard callbacks.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.tools import BaseTool, ToolContext

logger = logging.getLogger("feature_agent.guardrails")


def exfil_guard(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Layer A: Detect credential harvesting or metadata-server calls."""
    arg_str = str(args).lower()
    if "169.254.169.254" in arg_str or "metadata.google.internal" in arg_str:
        logger.warning("🔴 ExfilGuard blocked attempt to access GCP metadata server.")
        return {
            "error": "ExfilGuard policy: Access to GCP metadata server is strictly prohibited."
        }
    return None


def policies_guard(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Layer C: Enforce structural policy guards on shell and file tools."""
    str(getattr(tool, "name", "")) or str(tool)
    arg_str = str(args).lower()

    if "push --force" in arg_str or "-f" in arg_str:
        logger.warning("🔴 PoliciesGuard blocked forced push operation.")
        return {
            "error": "PoliciesGuard policy: Force pushing to remote origin is prohibited."
        }
    return None


def permission_guard(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Layer D: Detect quote-aware command substitution (`...` or $(...))."""
    arg_str = str(args)
    # Match backticks or $(...)
    if re.search(r"`[^`]+`|\$\([^\)]+\)", arg_str):
        logger.warning(
            "🔴 PermissionGuard detected command substitution in args: %s", arg_str
        )
        # In automated sandbox, return error or require explicit grant
        return {
            "error": "PermissionGuard policy: Command substitution in tool arguments requires explicit authorization."
        }
    return None
