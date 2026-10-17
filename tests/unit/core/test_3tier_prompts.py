"""Unit tests for 3-Tier Prompt Architecture and Gemini Prefix Caching."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from webhook_agent.core.prompts import (
    STATIC_REVIEW_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    build_repo_profile_context,
    build_static_review_instruction,
    build_transient_turn_content,
    build_user_message,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_static_review_instruction_byte_stability() -> None:
    """Tier 1 instruction must be 100% byte-stable to allow Gemini context caching."""
    inst1 = build_static_review_instruction()
    inst2 = build_static_review_instruction()
    assert inst1 == inst2
    assert inst1 == STATIC_REVIEW_INSTRUCTION
    assert inst1 == SYSTEM_INSTRUCTION

    hash1 = hashlib.sha256(inst1.encode("utf-8")).hexdigest()
    hash2 = hashlib.sha256(inst2.encode("utf-8")).hexdigest()
    assert hash1 == hash2

    # Must contain zero transient dynamic variables
    for forbidden in ("PR Number:", "Current UTC Time:", "Head SHA:", "Before SHA:"):
        assert forbidden not in inst1


def test_static_review_instruction_fluff_free_and_positive_framing() -> None:
    """Tier 1 instruction must use dense, positive framing, confidence thresholds, and silence by default."""
    inst = build_static_review_instruction()

    # Core audit dimensions
    assert "Logic & Boundaries" in inst
    assert "Concurrency & Memory" in inst
    assert "Security & Secrets" in inst
    assert "Contract Integrity" in inst

    # Grounding & invariant principles
    assert "Diff-First Grounding" in inst
    assert "Silence by Default" in inst
    assert "confidence >= 8" in inst
    assert "non-NIT" in inst
    assert "CodeReviewResponse" in inst
    assert "SyncReviewResponse" in inst

    # Fluff elimination: no redundant negative shouting
    assert "NO CHEERLEADING" not in inst
    assert "NEVER write vague quality prose" not in inst


def test_build_repo_profile_context_semi_stable() -> None:
    """Tier 2 repository profile provides stable context across PRs targeting the same repo."""
    profile = build_repo_profile_context("cgj8702-org/hannibal-hub-agents")
    assert "### Repository Profile: cgj8702-org/hannibal-hub-agents" in profile
    assert "Primary Stack: Python 3.13" in profile
    assert "Zero-Bypass Architecture" in profile

    # With Memory Bank heuristics
    heuristics = [
        "All model calls must be wrapped in offline mocks in tests/unit.",
        "Tools must adhere to Zero-Mutation Invariant.",
    ]
    profile_with_heuristics = build_repo_profile_context(
        "cgj8702-org/hannibal-hub-agents", memory_bank_heuristics=heuristics
    )
    assert "### Repository Review Heuristics (Memory Bank)" in profile_with_heuristics
    assert "All model calls must be wrapped in offline mocks" in profile_with_heuristics
    assert "Zero-Mutation Invariant" in profile_with_heuristics


def test_build_transient_turn_content() -> None:
    """Tier 3 transient turn content extracts event-specific diffs and metadata."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "sender": {"login": "test-dev"},
        "raw_payload": {
            "pull_request": {
                "number": 99,
                "title": "Add caching layer",
                "body": "Implements Tier 1 prefix caching",
            },
            "pr_diff": "--- a/cache.py\n+++ b/cache.py\n@@ -1,2 +1,2 @@\n- old\n+ new",
            "changed_files": ["cache.py"],
        },
    }

    transient = build_transient_turn_content(event_data)
    assert "Canonical Event: pull_request.opened" in transient
    assert "Sender: test-dev" in transient
    assert "PR Number: 99" in transient
    assert "PR Body" not in transient
    assert "Full PR Diff (Accumulated State):" in transient
    assert "ACTION DIRECTIVE: ADAPTIVE FORMAL AUDIT" in transient


def test_build_user_message_tier_assembly() -> None:
    """build_user_message cleanly integrates Tier 2 repository profile and Tier 3 transient content."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "cgj8702-org/hannibal-hub-agents"},
        "sender": {"login": "test-dev"},
        "raw_payload": {
            "pull_request": {
                "number": 101,
                "title": "Clean prompt architecture",
            },
            "pr_diff": "+ # New code",
            "changed_files": ["main.py"],
        },
    }

    msg = build_user_message(event_data)
    assert msg.role == "user"
    assert len(msg.parts) == 1
    text = msg.parts[0].text or ""

    # Tier 2 is prepended
    assert "### Repository Profile: cgj8702-org/hannibal-hub-agents" in text

    # Tier 3 follows
    assert "Canonical Event: pull_request.opened" in text
    assert "PR Number: 101" in text
