"""Tests for fail-closed deterministic dimensions when analysis did not run (#443)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from webhook_agent.review.review_enforcer import _enforce_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_diff_touches_python() -> None:
    """Only Python diffs are in scope for the Contract Integrity scan."""
    from webhook_agent.review.review_enforcer import _diff_touches_python

    py_diff = (
        "diff --git a/src/api.py b/src/api.py\n+++ b/src/api.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    assert _diff_touches_python(py_diff) is True

    pyi_diff = "diff --git a/src/api.pyi b/src/api.pyi\n+++ b/src/api.pyi\n"
    assert _diff_touches_python(pyi_diff) is True

    docs_diff = "diff --git a/README.md b/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-x\n+y\n"
    assert _diff_touches_python(docs_diff) is False

    assert _diff_touches_python("") is False


def test_missing_contract_floor_on_code_diff_fails_closed() -> None:
    """#443: no deterministic analysis + a code diff must not leave the model's PASS standing."""
    sync_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "summary": "Synchronized update with all prior findings resolved.",
        "resolutions": [
            {
                "item_description": "Initial bug",
                "status": "RESOLVED",
                "evidence": "Fixed in hunk",
            }
        ],
        "scorecard": [
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Model claimed clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    # No deterministic_contract_scorecard_item: this mirrors the 02:14 sync review, where the
    # PR workspace was never built so evaluate_contract_integrity() had nothing to scan.
    state = {
        "deterministic_diff_text": (
            "diff --git a/src/api.py b/src/api.py\n"
            "+++ b/src/api.py\n"
            "@@ -1 +1 @@\n"
            "-def f(a):\n"
            "+def f(a, b):\n"
        )
    }

    rendered_body, _enforced_verdict, _ = _enforce_verdict(
        json.dumps(sync_dict), "APPROVE", review_mode="sync", state=state
    )

    assert "`❓ UNKNOWN`" in rendered_body
    assert "did not run for this review" in rendered_body
    for dim in ("Contract Integrity", "Security & Secrets", "Test Coverage & Regressions"):
        assert dim in rendered_body


def test_missing_contract_floor_on_docs_diff_is_not_flagged() -> None:
    """A docs-only diff has nothing for the contract scan to verify, so no UNKNOWN is injected."""
    sync_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "summary": "Synchronized documentation update.",
        "resolutions": [
            {
                "item_description": "Doc typo",
                "status": "RESOLVED",
                "evidence": "Reworded",
            }
        ],
        "scorecard": [
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "No code touched."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    state = {
        "deterministic_diff_text": (
            "diff --git a/README.md b/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
        )
    }

    rendered_body, _enforced_verdict, _ = _enforce_verdict(
        json.dumps(sync_dict), "APPROVE", review_mode="sync", state=state
    )

    assert "did not run for this review" not in rendered_body
