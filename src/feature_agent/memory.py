"""Memory Bank auto-capture, memory pre-fetch, and skill curation for feature_agent.

Implements long-horizon-harness memory self-improvement loop primitives.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context

logger = logging.getLogger("feature_agent.memory")


def auto_capture_callback(callback_context: CallbackContext) -> None:
    """Post-turn memory sync callback: saves session events to Memory Bank."""
    try:
        if hasattr(callback_context, "add_session_to_memory"):
            callback_context.add_session_to_memory()
            logger.info("🧠 Auto-captured session turn into Memory Bank.")
    except Exception as exc:
        logger.debug("Memory capture skipped: %s", exc)


def search_memory_bank(ctx: Context, query: str) -> str:
    """PreloadMemoryTool: Search cross-session Memory Bank for codebase facts and preferences."""
    state = getattr(ctx, "state", {}) or {}
    memories = state.get("memory_bank_cache", [])
    if isinstance(memories, list) and memories:
        hits = [m for m in memories if query.lower() in str(m).lower()]
        if hits:
            return f"Memory hits for '{query}':\n" + "\n".join(str(h) for h in hits[:5])
    return f"No cross-session memory entries found for '{query}'."


def skill_curator_callback(callback_context: CallbackContext) -> None:
    """Discover .agents/skills/*/SKILL.md folders and curate into session state."""
    try:
        skills_dir = Path(".agents/skills")
        if not skills_dir.exists():
            return

        discovered_skills: list[dict[str, str]] = []
        for skill_md in skills_dir.glob("*/SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")
                discovered_skills.append(
                    {
                        "name": skill_md.parent.name,
                        "path": str(skill_md),
                        "summary": content.splitlines()[0] if content else "",
                    }
                )
            except Exception:
                pass

        if discovered_skills:
            callback_context.state["discovered_skills"] = discovered_skills
            logger.info(
                "📚 Curated %d SKILL.md skills into session memory.",
                len(discovered_skills),
            )
    except Exception as exc:
        logger.debug("Skill curation skipped: %s", exc)
