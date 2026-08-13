"""Shared data types for the webhook agent pipeline.

This module was renamed from `types.py` to avoid shadowing the Python
standard-library `types` module which can cause ImportError for code that
imports `types` unqualified. Keep this file minimal and focused on package
types only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ActionResult:
    """Result of executing a single agent tool."""

    tool: str
    success: bool
    detail: str


# Backwards-compat import name for a transitional period when other
# modules may still import from .types — they should be updated to
# import from .webhook_types instead.
ActionResultAlias = ActionResult
