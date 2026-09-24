"""Deterministic, fail-closed PR scope safety routing.

The model router may classify reviewed work, but it may only grant the
``dev_docs`` shortcut when every changed path is a documentation file. Missing,
unparseable, executable, configuration, test, or development-code changes route
through the full code auditor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .diff_filter import split_diff_into_sections

DeterministicScope = Literal["dev_docs", "core_backend"]

DOCUMENTATION_SUFFIXES = {".adoc", ".md", ".mdx", ".rst", ".txt"}
NON_DOCUMENTATION_ROOT_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}


@dataclass(frozen=True)
class DeterministicScopeContext:
    """Safety route, complete changed-file inventory, and source diff."""

    scope: DeterministicScope
    changed_files: tuple[str, ...]
    diff: str


def deterministic_pr_scope(paths: list[str] | tuple[str, ...] | set[str]) -> DeterministicScope:
    """Return ``dev_docs`` only when every changed path is documentation-safe."""
    normalized = tuple(dict.fromkeys(str(path).strip().replace("\\", "/") for path in paths))
    if not normalized or any(not _is_documentation_path(path) for path in normalized):
        return "core_backend"
    return "dev_docs"


def deterministic_pr_scope_from_diff(diff: str) -> DeterministicScope:
    """Classify a unified/custom PR diff, failing closed when no path is known."""
    paths = tuple(
        dict.fromkeys(section.path for section in split_diff_into_sections(diff) if section.path)
    )
    return deterministic_pr_scope(paths)


def build_deterministic_scope_context(
    diff: str,
    changed_files: list[str] | tuple[str, ...] | set[str] | None = None,
) -> DeterministicScopeContext:
    """Build routing context from the full file inventory, falling back to diff parsing."""
    inventory = tuple(
        dict.fromkeys(
            str(path).strip().replace("\\", "/") for path in (changed_files or ()) if path
        )
    )
    if not inventory:
        inventory = tuple(
            dict.fromkeys(
                section.path for section in split_diff_into_sections(diff) if section.path
            )
        )
    return DeterministicScopeContext(
        scope=deterministic_pr_scope(inventory),
        changed_files=inventory,
        diff=diff,
    )


def _is_documentation_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    if not path or path == "unknown" or pure_path.suffix.lower() not in DOCUMENTATION_SUFFIXES:
        return False
    if pure_path.name in NON_DOCUMENTATION_ROOT_FILES:
        return False
    return not any(part in {".agents", ".githooks", ".github"} for part in pure_path.parts)
