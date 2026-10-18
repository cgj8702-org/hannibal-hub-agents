"""Unit tests for deterministic scorecard floor enforcement and Golden Asymmetry."""

from __future__ import annotations

import json
from typing import Any

import pytest

from webhook_agent.review.review_enforcer import (
    _enforce_verdict,
    apply_scorecard_floor,
    has_grounding_bypass_label,
    is_contract_dimension,
    is_security_dimension,
    is_test_coverage_dimension,
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "0")
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


def test_enforce_verdict_partitions_unverified_invariants() -> None:
    """Verify that unverified/semantic invariants are cleanly partitioned during enforcement."""
    from webhook_agent.review.metadata import extract_review_metadata

    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n+def run():\n+    pass\n"
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Clean PR with domain guarantees.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [
            "Worker execution logic verified (`src/app.py:1`)",
            "Gracefully handles transient network disconnections without crash.",
        ],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    rendered_body, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    assert verdict == "APPROVE"
    assert "### 3. Verified Invariants" in rendered_body
    assert "- [x] Worker execution logic verified (`src/app.py:1`)" in rendered_body
    assert "### 4. Semantic Invariants (Model-Asserted)" in rendered_body
    assert "* Gracefully handles transient network disconnections without crash." in rendered_body

    meta = extract_review_metadata(rendered_body)
    assert meta is not None
    assert meta["verified_invariants"] == ["Worker execution logic verified (`src/app.py:1`)"]
    assert meta["semantic_invariants"] == [
        "Gracefully handles transient network disconnections without crash."
    ]


def test_enforce_verdict_purges_hallucinated_semantic_invariants() -> None:
    """Verify that _enforce_verdict deterministically purges hallucinated semantic invariants and demotes unverified claims."""
    from webhook_agent.review.metadata import extract_review_metadata

    diff_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n+def run():\n+    pass\n"
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Clean PR with domain guarantees.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [
            "Worker execution logic verified (`src/app.py:1`)",
        ],
        "semantic_invariants": [
            "Gracefully handles transient network disconnections without crash.",
            "Ensures valid transactions in `src/nonexistent/phantom_db.py:42`",
            "Residual uncertainty regarding global cluster memory scaling",
            "Thread safety guaranteed by test_phantom_concurrency_race_condition",
            "Maintains that `PhantomDistributedMutex` prevents lock contention",
        ],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    rendered_body, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)

    assert verdict == "APPROVE"
    assert "### 3. Verified Invariants" in rendered_body
    assert "- [x] Worker execution logic verified (`src/app.py:1`)" in rendered_body
    assert "### 4. Semantic Invariants (Model-Asserted)" in rendered_body
    assert "* Gracefully handles transient network disconnections without crash." in rendered_body
    assert "phantom_db.py" not in rendered_body
    assert "test_phantom_concurrency_race_condition" not in rendered_body
    assert "PhantomDistributedMutex" not in rendered_body
    assert "### ❓ Unverified Claims & Residual Uncertainty" in rendered_body
    assert "* Residual uncertainty regarding global cluster memory scaling" in rendered_body

    meta = extract_review_metadata(rendered_body)
    assert meta is not None
    assert meta["verified_invariants"] == ["Worker execution logic verified (`src/app.py:1`)"]
    assert meta["semantic_invariants"] == [
        "Gracefully handles transient network disconnections without crash."
    ]
    assert meta["unverified_claims"] == [
        "Residual uncertainty regarding global cluster memory scaling"
    ]


def test_is_security_dimension() -> None:
    assert is_security_dimension("Security & Secrets") is True
    assert is_security_dimension("security") is True
    assert is_security_dimension("Secrets") is True
    assert is_security_dimension("Secret Keyword") is True
    assert is_security_dimension("Logic & Boundaries") is False
    assert is_security_dimension("Contract Integrity") is False


def test_apply_scorecard_floor_security_overrides_pass_to_fail() -> None:
    scorecard = [
        ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean"),
        ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Model claimed clean"),
    ]
    det_item = ScorecardItem(
        dimension="Security & Secrets",
        status="FAIL",
        notes="PROVEN: Hardcoded AWS secret key in src/auth.py:12",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert len(floored) == 2
    sec = next(i for i in floored if i.dimension == "Security & Secrets")
    assert sec.status == "FAIL"
    assert "PROVEN: Hardcoded AWS secret key in src/auth.py:12" in sec.notes


def test_apply_scorecard_floor_security_overrides_pass_to_warn() -> None:
    scorecard = [
        ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Model clean"),
    ]
    det_item = ScorecardItem(
        dimension="Security & Secrets",
        status="WARN",
        notes="SUSPECTED: Low confidence pattern match",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "WARN"
    assert "SUSPECTED: Low confidence pattern match" in floored[0].notes


def test_apply_scorecard_floor_security_preserves_model_fail_when_deterministic_is_pass() -> None:
    # Golden Asymmetry: PROVEN PASS does not override model's FAIL
    scorecard = [
        ScorecardItem(
            dimension="Security & Secrets",
            status="FAIL",
            notes="Model caught SSRF vulnerability via user parameter",
        ),
    ]
    det_item = ScorecardItem(
        dimension="Security & Secrets",
        status="PASS",
        notes="PASS: 0 hardcoded secrets or static security defects",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "FAIL"
    assert "SSRF vulnerability" in floored[0].notes


def test_enforce_verdict_locks_approve_to_request_changes_via_state_security_fail() -> None:
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation is clean and verified.",
        "confidence": 5,
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Clean."},
            {"dimension": "Concurrency & State", "status": "PASS", "notes": "Thread-safe."},
            {"dimension": "Security & Secrets", "status": "PASS", "notes": "No issues."},
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "Compatible."},
            {"dimension": "Test Coverage & Regressions", "status": "PASS", "notes": "Covered."},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)

    state = {
        "deterministic_security_scorecard_item": {
            "dimension": "Security & Secrets",
            "status": "FAIL",
            "notes": "PROVEN: Hardcoded secret detected at src/secrets.py:42",
        }
    }

    rendered_body, enforced_verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "Security & Secrets" in rendered_body
    assert "PROVEN: Hardcoded secret detected at src/secrets.py:42" in rendered_body


def test_enforce_verdict_direct_deterministic_security_item_param() -> None:
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Clean pass.",
        "scorecard": [
            {"dimension": "Security & Secrets", "status": "PASS", "notes": "Clean"},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)
    det_sec_item = ScorecardItem(
        dimension="Security & Secrets",
        status="FAIL",
        notes="PROVEN: Insecure temp file usage (bandit B108)",
    )

    rendered_body, enforced_verdict, _ = _enforce_verdict(
        json_body, "APPROVE", deterministic_security_item=det_sec_item
    )
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "PROVEN: Insecure temp file usage (bandit B108)" in rendered_body


def test_enforce_verdict_sync_review_security_floor() -> None:
    sync_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "summary": "Synchronize update clean.",
        "resolutions": [
            {
                "item_description": "Fix memory leak",
                "status": "RESOLVED",
                "evidence": "Fixed in src/leak.py",
                "category": "CRITICAL",
            }
        ],
        "scorecard": [
            {"dimension": "Security & Secrets", "status": "PASS", "notes": "Model says clean"},
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(sync_dict)
    state = {
        "deterministic_security_scorecard_item": {
            "dimension": "Security & Secrets",
            "status": "FAIL",
            "notes": "PROVEN: AWS Secret Key detected in new diff",
        }
    }

    rendered_body, enforced_verdict, _ = _enforce_verdict(
        json_body, "APPROVE", review_mode="sync", state=state
    )
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "PROVEN: AWS Secret Key detected in new diff" in rendered_body


def test_is_test_coverage_dimension() -> None:
    assert is_test_coverage_dimension("Test Coverage & Regressions") is True
    assert is_test_coverage_dimension("test coverage") is True
    assert is_test_coverage_dimension("Tests & Regressions") is True
    assert is_test_coverage_dimension("Test Suite Coverage") is True
    assert is_test_coverage_dimension("Logic & Boundaries") is False
    assert is_test_coverage_dimension("Security & Secrets") is False
    assert is_test_coverage_dimension("Contract Integrity") is False


def test_apply_scorecard_floor_test_coverage_warn() -> None:
    scorecard = [
        ScorecardItem(
            dimension="Test Coverage & Regressions", status="PASS", notes="Model says clean"
        ),
    ]
    det_item = ScorecardItem(
        dimension="Test Coverage & Regressions",
        status="WARN",
        notes="Modified code contains 2 symbol(s) lacking direct test references",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "WARN"
    assert "lacking direct test references" in floored[0].notes


def test_apply_scorecard_floor_test_coverage_golden_asymmetry() -> None:
    scorecard = [
        ScorecardItem(
            dimension="Test Coverage & Regressions",
            status="FAIL",
            notes="Model caught critical regression in test logic",
        ),
    ]
    det_item = ScorecardItem(
        dimension="Test Coverage & Regressions",
        status="PASS",
        notes="Diff coverage 95%",
    )
    floored = apply_scorecard_floor(scorecard, det_item)
    assert floored[0].status == "FAIL"
    assert "Model caught critical regression in test logic" in floored[0].notes


def test_has_grounding_bypass_label() -> None:
    class MockLabel:
        def __init__(self, name: str):
            self.name = name

    class MockPR:
        def __init__(self, labels: list[Any]):
            self.labels = labels

    assert has_grounding_bypass_label(None) is False
    assert has_grounding_bypass_label(MockPR([])) is False
    assert has_grounding_bypass_label(MockPR([MockLabel("enhancement")])) is False
    assert has_grounding_bypass_label(MockPR([MockLabel("skip-grounding-gate")])) is True
    assert has_grounding_bypass_label(MockPR([MockLabel("skip-grounding")])) is True
    assert has_grounding_bypass_label(MockPR([MockLabel("bypass-grounding")])) is True
    assert has_grounding_bypass_label(MockPR(["skip-grounding-gate"])) is True


def test_enforce_verdict_test_coverage_warn_does_not_block_approval() -> None:
    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "All checks passed cleanly.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "OK"},
            {
                "dimension": "Test Coverage & Regressions",
                "status": "PASS",
                "notes": "Model says ok",
            },
        ],
        "critical_issues": [],
        "minor_suggestions": [],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_test_scorecard_item": {
            "dimension": "Test Coverage & Regressions",
            "status": "WARN",
            "notes": "Diff coverage is 65.0% (< 80%).",
        }
    }

    rendered_body, enforced_verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)
    # Dimension 5 produces soft WARN, never mechanical FAIL: APPROVE is preserved!
    assert enforced_verdict == "APPROVE"
    assert "65.0%" in rendered_body
    assert "⚠️ WARN" in rendered_body


def test_enforce_verdict_grounding_emergency_bypass_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRICT_REVIEW_GROUNDING", "1")

    class MockLabel:
        def __init__(self, name: str):
            self.name = name

    class MockPR:
        def __init__(self, labels: list[Any]):
            self.labels = labels
            self.head = None

        def get_reviews(self):
            return []

    review_dict: dict[str, Any] = {
        "verdict": "APPROVE",
        "executive_summary": "Approved changes.",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "OK"},
        ],
        "critical_issues": [],
        "minor_suggestions": [
            {
                "path": "src/nonexistent_file.py",
                "line": 99,
                "description": "Hallucinated file finding",
                "suggested_fix": "fix()",
            }
        ],
    }
    json_body = json.dumps(review_dict)
    diff_text = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"

    # 1. Without emergency bypass label: strict grounding downgrades APPROVE to COMMENT
    pr_without_bypass = MockPR([])
    state_no_bypass = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/main.py"],
    }
    _, verdict_no_bypass, _ = _enforce_verdict(
        json_body,
        "APPROVE",
        pr=pr_without_bypass,
        state=state_no_bypass,
    )
    assert verdict_no_bypass == "COMMENT"

    # 2. With emergency bypass label: APPROVE is preserved despite ungrounded citations!
    pr_with_bypass = MockPR([MockLabel("skip-grounding-gate")])
    state_with_bypass = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/main.py"],
    }
    _, verdict_with_bypass, _ = _enforce_verdict(
        json_body,
        "APPROVE",
        pr=pr_with_bypass,
        state=state_with_bypass,
    )
    assert verdict_with_bypass == "APPROVE"


def test_enforce_verdict_recovers_scorecard_invariants() -> None:
    """_enforce_verdict must recover verified invariants from scorecard citations when none provided."""
    from webhook_agent.review.metadata import extract_review_metadata

    diff_text = """--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,3 @@
-def old():
+def new_feature():
"""
    review_dict = {
        "verdict": "APPROVE",
        "executive_summary": "Implementation clean.",
        "scorecard": [
            {
                "dimension": "Logic & Boundaries",
                "status": "PASS",
                "notes": "Verified at (src/app.py:10).",
            },
        ],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [],
    }
    json_body = json.dumps(review_dict)
    state = {
        "deterministic_diff_text": diff_text,
        "deterministic_changed_files": ["src/app.py"],
    }

    rendered_body, verdict, _ = _enforce_verdict(json_body, "APPROVE", state=state)
    assert verdict == "APPROVE"
    assert "### 3. Verified Invariants" in rendered_body
    assert "- [x] src/app.py:10: Logic & Boundaries check verified in PR diff." in rendered_body

    meta = extract_review_metadata(rendered_body)
    assert meta is not None
    assert meta["verified_invariants"] == [
        "src/app.py:10: Logic & Boundaries check verified in PR diff."
    ]
