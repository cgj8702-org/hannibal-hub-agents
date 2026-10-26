"""Unit tests for workspace-aware and error-aware deterministic scorecard fallbacks."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.core.prompts import build_transient_turn_content
from webhook_agent.environment.context import use_environment
from webhook_agent.review.review_enforcer import _enforce_verdict, _unverified_item

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_unverified_item_custom_reason() -> None:
    """_unverified_item formats note with the dynamic reason provided."""
    item_default = _unverified_item("Contract Integrity")
    assert item_default.status == "UNKNOWN"
    assert "deterministic analysis did not run" in item_default.notes

    item_custom = _unverified_item(
        "Security & Secrets", reason="no active PR workspace environment"
    )
    assert item_custom.status == "UNKNOWN"
    assert "(no active PR workspace environment)" in item_custom.notes


def test_enforce_verdict_reports_workspace_present_or_absent() -> None:
    """_enforce_verdict distinguishes between workspace present vs absent in UNVERIFIED notes."""
    sync_dict = {
        "verdict": "APPROVE",
        "summary": "Synchronized update.",
        "resolutions": [],
        "scorecard": [
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    state = {
        "deterministic_diff_text": (
            "diff --git a/src/api.py b/src/api.py\n+++ b/src/api.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        )
    }

    # Case 1: No workspace active
    rendered_no_ws, _, _ = _enforce_verdict(
        json.dumps(sync_dict), "APPROVE", review_mode="sync", state=state
    )
    assert "no active PR workspace environment" in rendered_no_ws

    # Case 2: Active workspace present
    fake_env = MagicMock()
    fake_env.root = "/fake/root"
    with use_environment(fake_env):
        rendered_with_ws, _, _ = _enforce_verdict(
            json.dumps(sync_dict), "APPROVE", review_mode="sync", state=state
        )
        assert "deterministic analysis not produced" in rendered_with_ws


def test_build_transient_turn_content_external_repo_analysis_exception() -> None:
    """When analysis throws an exception in an active workspace, specific error is recorded."""
    event_data = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "external-org/external-repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 10},
            "pr_diff": "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1",
            "changed_files": ["foo.py"],
        },
    }

    fake_env = MagicMock()
    fake_env.root = "/fake/workspace"

    with (
        use_environment(fake_env),
        patch(
            "webhook_agent.analysis.symbol_graph.SymbolImpactAnalyzer.analyze_modified_files",
            side_effect=RuntimeError("AST syntax boom"),
        ),
    ):
        _ = build_transient_turn_content(event_data)

    contract_item = event_data.get("deterministic_contract_scorecard_item")
    assert contract_item is not None
    assert contract_item["status"] == "UNKNOWN"
    assert "AST syntax boom" in contract_item["notes"]


def test_build_transient_turn_content_security_scanner_exception() -> None:
    """When security scanner throws an exception, specific error is recorded in scorecard item."""
    event_data = {
        "canonical": "pull_request.opened",
        "repository": {"full_name": "external-org/external-repo"},
        "sender": {"login": "dev"},
        "raw_payload": {
            "pull_request": {"number": 10},
            "pr_diff": "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1",
            "changed_files": ["foo.py"],
        },
    }

    with (
        patch(
            "webhook_agent.analysis.security_scanner.SecurityScanner.scan",
            side_effect=ValueError("Secret scan failure"),
        ),
    ):
        _ = build_transient_turn_content(event_data)

    sec_item = event_data.get("deterministic_security_scorecard_item")
    assert sec_item is not None
    assert sec_item["status"] == "UNKNOWN"
    assert "Secret scan failure" in sec_item["notes"]
