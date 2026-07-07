"""Shared data types for the webhook agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ActionResult:
    """Result of executing a single agent tool."""

    tool: str
    success: bool
    detail: str
