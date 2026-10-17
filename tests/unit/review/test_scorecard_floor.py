"""Unit tests for deterministic scorecard floor enforcement and Golden Asymmetry."""

from __future__ import annotations

import json
from typing import Any

import pytest

from webhook_agent.review.review_enforcer import (
    _enforce_verdict,
    apply_scorecard_floor,
    is_contract_dimension,
)
from webhook_agent.review.schemas import ScorecardItem

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_contract_dimension() -> None:
    assert is_contract_dimension("Contract Integrity") is True
    assert is_contract_dimension("Cross-File Contracts") is True
    assert is_contract_dimension("contract") is True
    assert is_contract_dimension("API Contract & Compatibility") is True
    assert is_contract_dimension("Logic & Boundaries") is False
    assert is_contract_dimension("Security & Secrets") is False
    assert is_contract_dimension("Test Coverage & Regressions") is False


def test_apply_scorecard_floor_overrides_pass_to_fail() -> None:
    scorecard = [
        ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Logic verified"),
        ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Model claimed clean"),
    ]
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="FAIL",
        notes="PROVEN: Broken call site at src/caller.py:10",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert len(floored) == 2
    contract = next(i for i in floored if i.dimension == "Contract Integrity")
    assert contract.status == "FAIL"
    assert "PROVEN: Broken call site at src/caller.py:10" in contract.notes


def test_apply_scorecard_floor_overrides_warn_to_fail() -> None:
    scorecard = [
        ScorecardItem(dimension="Contract Integrity", status="WARN", notes="Model warned"),
    ]
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="FAIL",
        notes="PROVEN: Broken call site at src/caller.py:10",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "FAIL"


def test_apply_scorecard_floor_overrides_pass_to_warn() -> None:
    scorecard = [
        ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Model clean"),
    ]
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="WARN",
        notes="SUSPECTED: Dynamic call site mismatch",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "WARN"
    assert "SUSPECTED: Dynamic call site mismatch" in floored[0].notes


def test_apply_scorecard_floor_preserves_model_fail_when_deterministic_is_pass() -> None:
    # Golden Asymmetry: PROVEN PASS does not override model's FAIL (Rule 2)
    scorecard = [
        ScorecardItem(
            dimension="Contract Integrity",
            status="FAIL",
            notes="Model caught high-level domain contract violation",
        ),
    ]
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="PASS",
        notes="PASS: All AST signatures compatible",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "FAIL"
    assert "Model caught high-level domain contract violation" in floored[0].notes


def test_apply_scorecard_floor_appends_omitted_dimension() -> None:
    scorecard = [
        ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean"),
    ]
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="FAIL",
        notes="PROVEN: Broken call site",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert len(floored) == 2
    assert floored[0].dimension == "Logic & Boundaries"
    assert floored[1].dimension == "Contract Integrity"
    assert floored[1].status == "FAIL"


def test_enforce_verdict_locks_approve_to_request_changes_via_state_contract_fail() -> None:
    # Model generates clean APPROVE review with all PASS in scorecard
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation is clean and verified.",
        "confidence": 5,
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Model assumed clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)

    # State records deterministic pre-audit contract integrity as FAIL
    state = {
        "deterministic_contract_scorecard_item": {
            "dimension": "Contract Integrity",
            "status": "FAIL",
            "notes": "PROVEN: Broken call site(s) detected for 'process_event' at src/worker.py:45.",
        }
    }

    rendered_body, enforced_verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    # Mechanical verdict MUST be REQUEST_CHANGES due to deterministic floor
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "## 🛡️ Code Review: `REQUEST_CHANGES`" in rendered_body
    assert "`❌ FAIL`" in rendered_body
    assert (
        "PROVEN: Broken call site(s) detected for 'process_event' at src/worker.py:45"
        in rendered_body
    )


def test_enforce_verdict_direct_deterministic_contract_item_param() -> None:
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Feature ready.",
        "scorecard": [
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)
    det_item = ScorecardItem(
        dimension="Contract Integrity",
        status="FAIL",
        notes="PROVEN: Signature mismatch at src/api.py:12.",
    )

    rendered_body, enforced_verdict, _ = _enforce_verdict(
        json_body, "APPROVE", deterministic_contract_item=det_item
    )
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "PROVEN: Signature mismatch at src/api.py:12." in rendered_body


def test_enforce_verdict_sync_review_contract_floor() -> None:
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
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Resolved."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(sync_dict)
    state = {
        "deterministic_contract_scorecard_item": {
            "dimension": "Contract Integrity",
            "status": "FAIL",
            "notes": "PROVEN: Caller at tests/unit/test_app.py:50 broken by refactor.",
        }
    }

    rendered_body, enforced_verdict, _ = _enforce_verdict(
        json_body, "APPROVE", review_mode="sync", state=state
    )
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "PROVEN: Caller at tests/unit/test_app.py:50 broken by refactor." in rendered_body


def test_enforce_verdict_text_fallback_contract_fail() -> None:
    text_review = """
    ### Executive Summary
    Implementation looks good, no issues detected.

    ### 4. Critical Issues
    * None found.
    """
    state = {
        "deterministic_contract_scorecard_item": {
            "dimension": "Contract Integrity",
            "status": "FAIL",
            "notes": "PROVEN: Broken call site in src/main.py:15.",
        }
    }
    rendered_body, enforced_verdict, _ = _enforce_verdict(text_review, "APPROVE", state=state)
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "PROVEN: Broken call site in src/main.py:15." in rendered_body


def test_enforce_verdict_clean_pass_stays_approve() -> None:
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Clean and robust implementation.",
        "confidence": 5,
        "scorecard": [
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Compatible."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_contract_scorecard_item": {
            "dimension": "Contract Integrity",
            "status": "PASS",
            "notes": "PASS: All cross-file signatures and call sites compatible.",
        }
    }
    rendered_body, enforced_verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)
    assert enforced_verdict == "APPROVE"
    assert "`✅ PASS`" in rendered_body


def test_enforce_verdict_citation_grounding_shadow_mode_logs_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n+def run():\n+    pass\n"
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation verified.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "src/nonexistent.py",
                "line": 42,
                "description": "Hallucinated file citation",
                "suggested_fix": "fix()",
            }
        ],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    import logging

    with caplog.at_level(logging.INFO):
        _, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    # In Shadow Mode (default): verdict is preserved as APPROVE
    assert verdict == "APPROVE"
    # But telemetry log MUST be emitted
    assert "🔎 [Grounding Telemetry]" in caplog.text
    assert "hard:path_not_changed" in caplog.text


def test_enforce_verdict_citation_grounding_strict_mode_downgrades_to_comment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")

    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n+def run():\n+    pass\n"
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation clean.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "src/ghost_file.py",
                "line": 10,
                "description": "Fabricated path",
                "suggested_fix": "fix()",
            }
        ],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    import logging

    with caplog.at_level(logging.INFO):
        _, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    # In Strict Mode: Hard citation failure forces downgrade to COMMENT
    assert verdict == "COMMENT"
    assert "Strict Grounding Gate: Hard citation failures" in caplog.text
    assert "Downgrading APPROVE to COMMENT" in caplog.text


def test_enforce_verdict_citation_grounding_clean_citations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n+def run():\n+    pass\n"
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation clean.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "src/app.py",
                "line": 1,
                "description": "Grounded comment",
                "suggested_fix": "def run():",
            }
        ],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    import logging

    with caplog.at_level(logging.INFO):
        _, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    assert verdict == "APPROVE"
    assert "🔎 [Grounding Telemetry]" not in caplog.text
