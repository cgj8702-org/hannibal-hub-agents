"""Unit tests for Pydantic schemas, mechanical verdict math, and deterministic Markdown rendering."""

import pytest
from webhook_agent.formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    render_code_review_markdown,
    render_sync_review_markdown,
)
from webhook_agent.schemas import (
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    Scorecard,
    ScorecardEvidence,
    SyncResolutionItem,
    SyncReviewResponse,
)
from webhook_agent.webhook_agent import _enforce_verdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def valid_code_review_pass() -> CodeReviewResponse:
    return CodeReviewResponse(
        executive_summary="Clean feature implementation with full unit test coverage.",
        scorecard=Scorecard(
            correctness=5, security=5, performance=4, readability=5, test_coverage=4
        ),
        scorecard_evidence=ScorecardEvidence(
            correctness="Logic is sound.",
            security="No credential leaks.",
            performance="O(N) time complexity.",
            readability="Clean naming.",
            test_coverage="100% test coverage.",
        ),
        confidence=5,
        risks_and_edge_cases=[
            RiskItem(
                risk="High traffic burst latency.",
                recommendation="Monitor Cloud Run concurrency metrics.",
            )
        ],
        critical_issues=[],
        minor_suggestions=[],
        context_gaps=[],
    )


def test_calculate_strict_verdict_approve(valid_code_review_pass):
    verdict = calculate_strict_verdict(valid_code_review_pass)
    assert verdict == "APPROVE"


def test_calculate_strict_verdict_low_test_coverage(valid_code_review_pass):
    valid_code_review_pass.scorecard.test_coverage = 3
    verdict = calculate_strict_verdict(valid_code_review_pass)
    assert verdict == "REQUEST_CHANGES"


def test_calculate_strict_verdict_critical_issue(valid_code_review_pass):
    valid_code_review_pass.critical_issues.append(
        IssueItem(
            path="src/auth.py",
            line=42,
            description="Null pointer exception on missing user token",
            suggested_fix="Add if token is None check",
        )
    )
    verdict = calculate_strict_verdict(valid_code_review_pass)
    assert verdict == "REQUEST_CHANGES"


def test_calculate_strict_verdict_low_confidence(valid_code_review_pass):
    valid_code_review_pass.confidence = 3
    verdict = calculate_strict_verdict(valid_code_review_pass)
    assert verdict == "COMMENT"


def test_render_code_review_markdown(valid_code_review_pass):
    md = render_code_review_markdown(valid_code_review_pass)
    assert "# 🛡️ Hannibal Hub Audit Report: `APPROVE`" in md
    assert "Quality Scorecard Average:** `4.6/5.0`" in md
    assert "Correctness: 5/5" in md
    assert "Tests: 4/5" in md


def test_enforce_verdict_with_raw_json(valid_code_review_pass):
    json_str = valid_code_review_pass.model_dump_json()
    rendered_md, verdict = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "APPROVE"
    assert "Hannibal Hub Audit Report" in rendered_md


def test_enforce_verdict_with_codeblock_json(valid_code_review_pass):
    valid_code_review_pass.scorecard.correctness = 2
    json_str = f"```json\n{valid_code_review_pass.model_dump_json()}\n```"
    rendered_md, verdict = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "REQUEST_CHANGES"
    assert "# 🛡️ Hannibal Hub Audit Report: `REQUEST_CHANGES`" in rendered_md


def test_sync_review_rendering():
    sync_resp = SyncReviewResponse(
        summary="Incremental fixes applied for PR review feedback.",
        resolutions=[
            SyncResolutionItem(
                item_description="Null check missing in auth.py",
                status="RESOLVED",
                evidence="auth.py:L45 added guard statement",
            )
        ],
        new_findings=[],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "APPROVE"

    md = render_sync_review_markdown(sync_resp, verdict)
    assert "# Pull Request Synchronization Review Update" in md
    assert "✅ **[RESOLVED]**" in md


def test_enforce_verdict_with_loose_schema_drift_json():
    """Verify self-healing normalizer recovers from LLM schema drift (strings instead of RiskItems, architecture instead of correctness)."""
    loose_json = """{
      "executive_summary": "Pull Request #81 centralizes Google ADK Gemini model instantiations.",
      "scorecard": {
        "architecture": 5,
        "security": 4,
        "performance": 4,
        "reliability": 4,
        "readability": 5,
        "test_coverage": 4
      },
      "scorecard_evidence": {
        "architecture": "Successfully extracts RateLimitedGemini",
        "security": "API keys preserved",
        "performance": "Maintains token estimation",
        "reliability": "Clean fallback imports",
        "readability": "Well-documented functions",
        "test_coverage": "Includes unit tests"
      },
      "confidence": 5,
      "risks_and_edge_cases": [
        "Circular import risk: RateLimitedGemini imports get_active_model lazily"
      ],
      "critical_issues": [],
      "minor_suggestions": [
        "Consider adding a unit test"
      ],
      "context_gaps": []
    }"""
    rendered_md, verdict = _enforce_verdict(loose_json, "APPROVE")
    assert verdict == "APPROVE"
    assert "Hannibal Hub Audit Report" in rendered_md
    assert "Correctness: 5/5" in rendered_md
    assert "Circular import risk" in rendered_md


def test_enforce_verdict_with_loose_sync_review_json():
    """Verify self-healing normalizer recovers from SyncReviewResponse schema drift (issue key instead of item_description)."""
    loose_sync_json = """{
      "summary": "The author successfully resolved the previous review feedback by adding robust fallback path resolution.",
      "resolutions": [
        {
          "issue": "Asset Path Resolution Mismatch between FS.DATA and src/hannibal/assets",
          "status": "RESOLVED",
          "evidence": "Added importlib.resources.files fallback."
        }
      ],
      "new_findings": [
        {
          "severity": "LOW",
          "category": "MAINTAINABILITY",
          "title": "Repository Size Impact",
          "description": "Committing tokenizer asset bloats history."
        }
      ],
      "confidence": 5
    }"""
    rendered_md, verdict = _enforce_verdict(loose_sync_json, "APPROVE")
    assert verdict == "APPROVE"
    assert "# Pull Request Synchronization Review Update" in rendered_md
    assert "Asset Path Resolution Mismatch" in rendered_md
    assert "✅ **[RESOLVED]**" in rendered_md
    assert "[MAINTAINABILITY] Committing tokenizer asset bloats history." in rendered_md


def test_calculate_sync_verdict_blocking_new_finding():
    """Verify that a critical/blocking issue forces REQUEST_CHANGES in sync reviews."""
    sync_resp = SyncReviewResponse(
        summary="PR update introduced a critical security issue.",
        resolutions=[
            SyncResolutionItem(
                item_description="Previous minor issue fixed.",
                status="RESOLVED",
                evidence="Fixed in L20",
            )
        ],
        critical_issues=[
            IssueItem(
                path="src/auth.py",
                line=12,
                description="Security vulnerability: Token validation bypassed.",
                suggested_fix="Restore token validation check.",
            )
        ],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "REQUEST_CHANGES"


def test_parse_text_review_to_dict():
    """Verify that parse_text_review_to_dict correctly extracts structured data from loose text reviews."""
    from webhook_agent.formatter import parse_text_review_to_dict

    text_review = """# Code Review Report

### 1. Executive Summary
* **Goal of the PR:** Add logging telemetry and update configuration logic.

### 2. Scorecard Breakdown
* **Code Correctness:** 4/5 — Correct logic with global project assignment.
* **Security & Privacy:** 4/5 — Default fallback is acceptable.
* **Performance & Scale:** 5/5 — Fast initialization.
* **Readability & Style:** 4/5 — Clean code.
* **Test Coverage:** 2/5 — Lacks unit test coverage for new helper function.

### 4. Mandatory Risk & Edge-Case Analysis
* **Potential Edge Case / Risk:** Concurrency race condition on global assignment.
* **Recommended Safeguard:** Use thread locking or singleton pattern.

### 5. Key Issues & Action Items
#### 🔴 Critical
* `src/webhook_agent/formatter.py`: Missing test coverage for parse_text_review_to_dict.

#### 🟡 Minor / Refactoring
* `src/webhook_agent/webhook_agent.py`: Consider moving fallback constant.

Confidence: 4/5
"""
    data = parse_text_review_to_dict(text_review)
    assert (
        data["executive_summary"]
        == "Add logging telemetry and update configuration logic."
    )
    assert data["scorecard"]["correctness"] == 4
    assert data["scorecard"]["test_coverage"] == 2
    assert len(data["risks_and_edge_cases"]) >= 1
    assert (
        data["risks_and_edge_cases"][0]["risk"]
        == "Concurrency race condition on global assignment."
    )
    assert len(data["critical_issues"]) >= 1
    assert "src/webhook_agent/formatter.py" in data["critical_issues"][0]["path"]
    assert "Missing test coverage" in data["critical_issues"][0]["description"]
    assert len(data["minor_suggestions"]) >= 1
    assert "src/webhook_agent/webhook_agent.py" in data["minor_suggestions"][0]["path"]
