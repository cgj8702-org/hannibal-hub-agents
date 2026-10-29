"""Unit tests for core.prompts prompt constructors and message builders."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from webhook_agent.core.prompts import (
    AUDITOR_CONTEXT_INSTRUCTION,
    CONVERSATIONAL_INSTRUCTION,
    MAX_INPUT_TOKENS,
    SYSTEM_INSTRUCTION,
    _truncate_input_for_tier,
    _truncate_text_to_token_limit,
    build_user_message,
    get_max_input_tokens,
)

pytestmark = [pytest.mark.integration, pytest.mark.webhook_agent]


def test_max_input_tokens_tier_resolution() -> None:
    """Test get_max_input_tokens respects paid vs free tier."""
    with patch("webhook_agent.core.prompts._resolve_tier", return_value="free"):
        assert get_max_input_tokens() == MAX_INPUT_TOKENS
        assert get_max_input_tokens() == 3500

    with patch("webhook_agent.core.prompts._resolve_tier", return_value="paid"):
        assert get_max_input_tokens() == 1000000


def test_truncation_helpers_preserve_text() -> None:
    """Test _truncate_input_for_tier and _truncate_text_to_token_limit return text unchanged."""
    text = "Clinical audit payload with critical invariants."
    assert _truncate_input_for_tier(text) == text
    assert _truncate_text_to_token_limit(text) == text


def test_build_user_message_labels_pr_body_as_unverified_author_claims() -> None:
    """PR body must be explicitly labeled as unverified author claims in build_user_message."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "action": "opened",
        "raw_payload": {
            "pull_request": {
                "number": 42,
                "title": "Fix race condition",
                "body": "Includes 31 unit tests and fixes worker race condition",
            },
        },
    }

    content = build_user_message(event_data)
    text = content.parts[0].text or ""
    assert "PR Body" not in text
    assert "Includes 31 unit tests" not in text


def test_system_instruction_contains_author_claim_grounding_directive() -> None:
    """SYSTEM_INSTRUCTION must explicitly direct auditor to use diff-first grounding."""
    assert "Diff-First Grounding" in SYSTEM_INSTRUCTION
    assert "untrusted self-descriptions" in SYSTEM_INSTRUCTION


def test_system_instruction_clause_group_1_pruned() -> None:
    """SYSTEM_INSTRUCTION clauses 6 & 7 are concise directives without redundant pipeline explanations."""
    assert (
        "Analyze the pre-compiled dossier, diff chunks, and contract impact embedded in your prompt."
        in SYSTEM_INSTRUCTION
    )
    assert (
        "The deterministic pre-audit compiler already executes syntax parsing"
        not in SYSTEM_INSTRUCTION
    )
    assert (
        "Do NOT call a review tool — the host pipeline deterministically parses"
        not in SYSTEM_INSTRUCTION
    )
    assert "Do NOT call a review tool" not in SYSTEM_INSTRUCTION
    assert "Do NOT call a `review` tool" not in SYSTEM_INSTRUCTION
    assert "`review`" in SYSTEM_INSTRUCTION


def test_system_instruction_clause_group_2_pruned() -> None:
    """SYSTEM_INSTRUCTION diff grounding section is streamlined since filter_echo_suggestions enforces echo filtering."""
    assert (
        "Findings must target directly observable defects or concrete code improvements anchored to visible diff lines."
        in SYSTEM_INSTRUCTION
    )
    assert (
        "Strictly prohibited from repeating existing code verbatim in `suggested_fix`"
        not in SYSTEM_INSTRUCTION
    )
    assert "NEVER copy existing code unchanged into `suggested_fix`" not in SYSTEM_INSTRUCTION
    assert "NEVER echo existing code lines unchanged into `suggested_fix`" not in SYSTEM_INSTRUCTION


def test_system_instruction_contains_adaptive_investigation_directive() -> None:
    """SYSTEM_INSTRUCTION must explicitly direct auditor on adaptive investigation and tool usage."""
    assert "Investigation & Tools (Adaptive Exploration)" in SYSTEM_INSTRUCTION
    assert "Fast-Path Single-Turn" in SYSTEM_INSTRUCTION
    assert "Proactive Multi-Turn Investigation" in SYSTEM_INSTRUCTION
    assert "search_files" in SYSTEM_INSTRUCTION
    assert "read_file" in SYSTEM_INSTRUCTION


def test_build_user_message_pull_request_opened() -> None:
    """Test build_user_message for PR opened event."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "action": "opened",
        "summary": "Fix race condition in worker loop",
        "raw_payload": {
            "pull_request": {
                "number": 42,
                "title": "Fix race condition",
                "body": "Resolves worker contention",
            },
            "pr_diff": "diff --git a/src/worker.py b/src/worker.py\n+ lock.acquire()",
            "changed_files": ["src/worker.py"],
        },
    }

    content = build_user_message(event_data)
    assert content.role == "user"
    assert len(content.parts) == 1
    assert content.parts[0].text is not None

    text = content.parts[0].text
    assert "Canonical Event: pull_request.opened" in text
    assert "Sender: developer" in text
    assert "Full PR Diff (Accumulated State):" in text
    assert "ACTION DIRECTIVE: ADAPTIVE FORMAL AUDIT" in text


def test_build_user_message_conversational_comment() -> None:
    """Test build_user_message for issue comment event."""
    event_data: dict[str, Any] = {
        "canonical": "issue_comment.created",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "action": "created",
        "summary": "User comment on PR",
        "raw_payload": {
            "issue": {"number": 10},
            "comment": {"body": "Could you check if this function is idempotent?"},
        },
    }

    content = build_user_message(event_data)
    assert content.role == "user"
    assert len(content.parts) == 1
    assert content.parts[0].text is not None

    text = content.parts[0].text
    assert "Canonical Event: issue_comment.created" in text
    assert "Sender: developer" in text
    assert "Comment: Could you check if this function is idempotent?" in text


def test_build_user_message_issues_unlabeled_owner_override() -> None:
    """Owner removing a label must surface action + label + OWNER OVERRIDE flag."""
    event_data: dict[str, Any] = {
        "canonical": "issues.unlabeled",
        "sender": {"login": "cgj8702"},
        "raw_payload": {
            "action": "unlabeled",
            "sender": {"login": "cgj8702"},
            "issue": {"number": 271, "title": "Jules task", "body": "Do the thing."},
            "label": {"name": "jules"},
        },
    }

    content = build_user_message(event_data)
    assert content.role == "user"
    assert content.parts[0].text is not None

    text = content.parts[0].text
    assert "Label Action: unlabeled" in text
    assert "Label Name: jules" in text
    assert "OWNER OVERRIDE" in text
    assert "cgj8702" in text


def test_build_user_message_issues_labeled_owner_override() -> None:
    """Owner adding a label must also flag OWNER OVERRIDE."""
    event_data: dict[str, Any] = {
        "canonical": "issues.labeled",
        "sender": {"login": "cgj8702"},
        "raw_payload": {
            "action": "labeled",
            "sender": {"login": "cgj8702"},
            "issue": {"number": 272, "title": "Jules task", "body": "Do the thing."},
            "label": {"name": "jules"},
        },
    }

    content = build_user_message(event_data)
    text = content.parts[0].text
    assert text is not None
    assert "Label Action: labeled" in text
    assert "Label Name: jules" in text
    assert "OWNER OVERRIDE" in text


def test_build_user_message_issues_unlabeled_non_owner_no_override() -> None:
    """Non-owner label events must surface label details without the override flag."""
    event_data: dict[str, Any] = {
        "canonical": "issues.unlabeled",
        "sender": {"login": "octocat"},
        "raw_payload": {
            "action": "unlabeled",
            "sender": {"login": "octocat"},
            "issue": {"number": 271, "title": "Jules task", "body": "Do the thing."},
            "label": {"name": "jules"},
        },
    }

    content = build_user_message(event_data)
    text = content.parts[0].text
    assert text is not None
    assert "Label Action: unlabeled" in text
    assert "Label Name: jules" in text
    assert "OWNER OVERRIDE" not in text


def test_instructions_exist_and_non_empty() -> None:
    """Test that all instruction prompt constants are non-empty strings."""
    assert len(SYSTEM_INSTRUCTION) > 100
    assert len(AUDITOR_CONTEXT_INSTRUCTION) > 100
    assert len(CONVERSATIONAL_INSTRUCTION) > 100


def test_webhook_agent_build_user_message_delegation_parity() -> None:
    """Verify WebhookAgent._build_user_message delegates identically to build_user_message."""
    from webhook_agent.core.agent_definition import WebhookAgent

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "raw_payload": {"pull_request": {"number": 123}},
    }
    agent = WebhookAgent(dry_run=True)
    delegated = agent._build_user_message(event_data)
    direct = build_user_message(event_data)
    assert delegated.role == direct.role
    assert delegated.parts[0].text == direct.parts[0].text


def test_build_user_message_injects_linked_issue_spec() -> None:
    """Verify linked_issue_spec is rendered in build_user_message when present."""
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "raw_payload": {
            "pull_request": {"number": 123},
            "linked_issue_spec": "Linked Issue #42: Must support timeout",
        },
    }
    content = build_user_message(event_data)
    text = content.parts[0].text or ""
    assert "Linked Issue / Task Specification" in text
    assert "Linked Issue #42: Must support timeout" in text


def test_build_user_message_omits_symbol_and_test_impact_when_no_workspace() -> None:
    """When no workspace is active, SymbolImpactAnalyzer and TestImpactAnalyzer sections are omitted."""
    from webhook_agent.environment.context import get_active_environment

    assert get_active_environment() is None

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "raw_payload": {
            "pull_request": {
                "number": 123,
                "base": {"sha": "base123"},
                "head": {"sha": "head123"},
            },
            "changed_files": ["src/service.py"],
            "pr_diff": "diff --git a/src/service.py b/src/service.py\n+def foo(): pass",
        },
    }
    content = build_user_message(event_data)
    text = content.parts[0].text or ""
    assert "Cross-File Contract & Symbol Impact Analysis" not in text


def test_build_user_message_runs_symbol_and_test_impact_when_workspace_active(
    tmp_path,
) -> None:
    """When workspace is active, SymbolImpactAnalyzer and TestImpactAnalyzer run against workspace root."""
    from unittest.mock import MagicMock

    from webhook_agent.environment import LocalEnvironment, use_environment

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "service.py").write_text("def run(a, b, new_req): pass\n")
    (src_dir / "caller.py").write_text("run(1, 2)\n")

    env = LocalEnvironment(tmp_path)
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_content = MagicMock()
    mock_content.decoded_content = b"def run(a, b): pass\n"
    mock_repo.get_contents.return_value = mock_content

    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "gh_client": mock_gh,
        "raw_payload": {
            "pull_request": {
                "number": 123,
                "base": {"sha": "base123"},
                "head": {"sha": "head123"},
            },
            "changed_files": ["src/service.py"],
            "pr_diff": "diff --git a/src/service.py b/src/service.py\n+def run(a, b, new_req): pass",
        },
    }

    with use_environment(env):
        content = build_user_message(event_data)

    text = content.parts[0].text or ""
    assert "Cross-File Contract & Symbol Impact Analysis" in text
    assert "MISSING_REQUIRED_ARG" in text
    assert "Dimension 4 (Contract Integrity) Evaluated Status" in text
    mock_repo.get_contents.assert_called_once_with("src/service.py", ref="base123")
    contract_item = event_data.get("deterministic_contract_scorecard_item")
    assert contract_item is not None
    assert contract_item["dimension"] == "Contract Integrity"
    assert contract_item["status"] == "FAIL"


def test_conversational_instruction_includes_system_mode_awareness() -> None:
    """Conversational instruction must explicitly document dual modes and prevent tool denial."""
    assert "System Architecture & Mode Awareness" in CONVERSATIONAL_INSTRUCTION
    assert "conversational_agent" in CONVERSATIONAL_INSTRUCTION
    assert "code_auditor" in CONVERSATIONAL_INSTRUCTION
    assert "review" in CONVERSATIONAL_INSTRUCTION


def test_build_user_message_injects_full_file_context_on_paid_tier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Paid Tier, full contents of modified Python files are injected for whole-file context."""
    from webhook_agent.environment import LocalEnvironment, use_environment

    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    service_code = "class PaymentService:\n    def process(self):\n        return True\n"
    (src_dir / "service.py").write_text(service_code)

    env = LocalEnvironment(tmp_path)
    event_data: dict[str, Any] = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "owner/hannibal-hub-agents"},
        "sender": {"login": "developer"},
        "raw_payload": {
            "pull_request": {"number": 42},
            "changed_files": ["src/service.py"],
            "pr_diff": "diff --git a/src/service.py b/src/service.py\n+        return True",
        },
    }

    with use_environment(env):
        content = build_user_message(event_data)

    text = content.parts[0].text or ""
    assert "Full Source File Context (Paid Tier High-Capacity Mode)" in text
    assert "class PaymentService:" in text
