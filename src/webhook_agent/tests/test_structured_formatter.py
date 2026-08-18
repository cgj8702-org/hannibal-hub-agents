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
    assert "# Code Review Report" in md
    assert "### 2. Scorecard Summary" in md
    assert "Code Correctness:** 5/5" in md
    assert "Test Coverage:** 4/5" in md
    assert "Overall Verdict:** APPROVE" in md


def test_enforce_verdict_with_raw_json(valid_code_review_pass):
    json_str = valid_code_review_pass.model_dump_json()
    rendered_md, verdict = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "APPROVE"
    assert "# Code Review Report" in rendered_md


def test_enforce_verdict_with_codeblock_json(valid_code_review_pass):
    valid_code_review_pass.scorecard.correctness = 2
    json_str = f"```json\n{valid_code_review_pass.model_dump_json()}\n```"
    rendered_md, verdict = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "REQUEST_CHANGES"
    assert "Overall Verdict:** REQUEST_CHANGES" in rendered_md


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
    assert "# Code Review Report" in rendered_md
    assert "Code Correctness:** 5/5" in rendered_md
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
    assert verdict == "REQUEST_CHANGES"
    assert "# Pull Request Synchronization Review Update" in rendered_md
    assert "Asset Path Resolution Mismatch" in rendered_md
    assert "✅ **[RESOLVED]**" in rendered_md
    assert "[MAINTAINABILITY] Committing tokenizer asset bloats history." in rendered_md
