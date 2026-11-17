"""Resolution verification for incremental pull request synchronization re-reviews.

Verifies resolution claims against current code diff and removal hunks.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from webhook_agent.tools.diff_tools import walk_right_side

from .citations import bare_path
from .heuristics import extract_bare_symbols, extract_cited_paths, extract_cited_symbols

logger = logging.getLogger("webhook_agent.review.grounding.resolutions")


def parse_removal_hunks(diff_text: str) -> dict[str, list[str]]:
    """Parse left-side removed lines (-) from unified diff text keyed by bare path."""
    removals: dict[str, list[str]] = {}
    current_file = ""
    for line in (diff_text or "").splitlines():
        if line.startswith("--- a/"):
            current_file = bare_path(line[6:].strip())
            removals.setdefault(current_file, [])
        elif line.startswith("+++ b/"):
            continue
        elif line.startswith("-") and not line.startswith("---"):
            if current_file:
                removals.setdefault(current_file, []).append(line[1:].strip())
    return removals


def verify_resolutions(
    resolutions: Sequence[Mapping[str, Any] | Any],
    diff_text: str,
    changed_paths: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Verify resolution claims against current code diff and removal hunks.

    A prior finding may be marked RESOLVED only if current code diff or removal hunks
    support the specific fix. If evidence or cited path/symbols are absent or ungrounded,
    status is flipped to UNRESOLVED.
    """
    anchors, line_text = walk_right_side(diff_text)
    removals = parse_removal_hunks(diff_text)
    if changed_paths is None:
        changed = set(line_text) | set(anchors) | set(removals)
    else:
        changed = {bare_path(p) for p in changed_paths if p}

    changed_lower = {c.lower(): c for c in changed}

    def _match_changed_paths(cp: str) -> list[str]:
        cp_l = bare_path(cp).lower()
        if cp_l in changed_lower:
            return [changed_lower[cp_l]]
        return [orig for lower, orig in changed_lower.items() if lower.endswith("/" + cp_l)]

    all_added_text = " ".join(
        txt for file_lines in line_text.values() for txt in file_lines.values()
    )
    all_removed_text = " ".join(
        line for file_removals in removals.values() for line in file_removals
    )

    verified_results: list[dict[str, Any]] = []

    for item in resolutions:
        if isinstance(item, dict):
            item_dict = dict(item)
        elif hasattr(item, "model_dump"):
            item_dict = item.model_dump()
        else:
            item_dict = {
                "item_description": getattr(item, "item_description", ""),
                "status": getattr(item, "status", "RESOLVED"),
                "evidence": getattr(item, "evidence", ""),
                "category": getattr(item, "category", "CRITICAL"),
            }

        status = item_dict.get("status", "RESOLVED")
        if status != "RESOLVED":
            verified_results.append(item_dict)
            continue

        desc = str(item_dict.get("item_description") or "")
        ev = str(item_dict.get("evidence") or "")
        raw_path = item_dict.get("path")
        item_path = str(raw_path).strip() if isinstance(raw_path, str) else ""
        combined_text = f"{desc} {ev}"

        cited_paths = extract_cited_paths(combined_text)
        if item_path:
            cited_paths.add(bare_path(item_path).lower())

        cited_symbols = extract_cited_symbols(combined_text) | extract_bare_symbols(combined_text)

        # 1. Path check (supports both full repo-relative paths and unique basenames)
        matched_target_files: set[str] = set()
        path_ok = True
        if cited_paths:
            for cp in cited_paths:
                matched_target_files.update(_match_changed_paths(cp))
            if not matched_target_files:
                path_ok = False

        # 2. Symbol / Evidence check
        evidence_ok = bool(cited_symbols)
        if cited_symbols:
            target_files = matched_target_files if matched_target_files else changed
            path_added_text = " ".join(
                txt for cp in target_files for txt in line_text.get(cp, {}).values()
            )
            path_removed_text = " ".join(
                line for cp in target_files for line in removals.get(cp, [])
            )
            if not matched_target_files:
                path_added_text = all_added_text
                path_removed_text = all_removed_text
            symbol_in_added = any(sym in path_added_text.lower() for sym in cited_symbols)
            symbol_in_removed = any(sym in path_removed_text.lower() for sym in cited_symbols)
            if not (symbol_in_added or symbol_in_removed):
                evidence_ok = False
        else:
            test_keywords = (
                "test coverage",
                "unit test",
                "untested",
                "missing test",
                "lack of test",
                "test suite",
                "tests for",
            )
            is_test_related = any(kw in combined_text.lower() for kw in test_keywords)
            if is_test_related and matched_target_files:
                for cp in matched_target_files:
                    if (
                        cp.startswith("tests/")
                        or "test_" in cp
                        or "_test." in cp
                        or cp.endswith("_test.py")
                    ) and bool(line_text.get(cp)):
                        evidence_ok = True
                        break

        if not path_ok or not evidence_ok:
            logger.info(
                "Resolution grounding demoted RESOLVED -> UNRESOLVED (path_ok=%s, evidence_ok=%s, cited_paths=%s, cited_symbols=%s)",
                path_ok,
                evidence_ok,
                sorted(cited_paths),
                sorted(cited_symbols),
            )
            item_dict["status"] = "UNRESOLVED"
            item_dict["grounding_note"] = (
                "Resolution not grounded in current code diff: cited path or evidence symbol absent from diff"
            )

        verified_results.append(item_dict)

    return verified_results
