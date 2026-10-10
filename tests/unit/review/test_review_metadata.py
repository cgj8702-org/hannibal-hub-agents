"""Unit tests for review metadata serialization, extraction, legacy parsing, and decoupled agent prompt formatting."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.review.metadata import (
    extract_review_metadata,
    format_findings_for_agent,
    get_actionable_findings,
    has_actionable_findings,
    parse_legacy_review_findings,
    serialize_review_metadata,
)
from webhook_agent.review.review_enforcer import _enforce_verdict
from webhook_agent.review.schemas import (
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    SyncResolutionItem,
    SyncReviewResponse,
    VerifiedInvariant,
)


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_metadata_roundtrip_serialization() -> None:
    """Ensure metadata serializes into HTML comments and deserializes cleanly."""
    original = {
        "version": 1,
        "type": "initial",
        "verdict": "REQUEST_CHANGES",
        "critical_issues": [
            {
                "file_path": "src/app.py",
                "line_number": 42,
                "description": "SQL injection vector",
                "suggested_fix": "db.execute(query, params)",
                "category": "CRITICAL",
            }
        ],
        "minor_suggestions": [
            {
                "file_path": "src/utils.py",
                "line_number": 10,
                "description": "Use isinstance instead of type",
                "suggested_fix": "isinstance(x, int)",
                "category": "SUGGESTION",
            }
        ],
        "risks": [
            {
                "risk": "Memory leak on unbounded map",
                "recommendation": "Use LRUCache",
                "category": "RISK",
            }
        ],
    }

    comment = serialize_review_metadata(original)
    assert comment.startswith("<!-- hannibal-review-metadata: ")
    assert comment.endswith(" -->")

    extracted = extract_review_metadata(f"## Header\n\nSome review body\n\n{comment}\n")
    assert extracted == original


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_extract_review_metadata_missing_or_invalid() -> None:
    """Ensure extract_review_metadata safely handles missing or malformed metadata."""
    assert extract_review_metadata("") is None
    assert extract_review_metadata("Just a normal review without comments") is None
    assert extract_review_metadata("<!-- hannibal-review-metadata: not-valid-json -->") is None


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_code_review_response_embeds_metadata() -> None:
    """Ensure CodeReviewResponse.to_markdown() embeds structured metadata."""
    cr = CodeReviewResponse(
        executive_summary="Architectural audit pass with 1 suggestion.",
        verdict="APPROVE",
        critical_issues=[],
        minor_suggestions=[
            IssueItem(
                path="src/main.py",
                line=25,
                description="Consider using a generator",
                suggested_fix="yield from items",
            )
        ],
        risks_and_edge_cases=[
            RiskItem(risk="High concurrency throughput", recommendation="Benchmark under load")
        ],
    )
    md = cr.to_markdown(verdict="APPROVE")
    assert "<!-- hannibal-review-metadata:" in md
    assert "* **Summary & Justification:**" not in md
    assert "### 1. Executive Summary\n\nArchitectural audit pass with 1 suggestion." in md

    meta = extract_review_metadata(md)
    assert meta is not None
    assert meta["version"] == 1
    assert meta["type"] == "initial"
    assert meta["verdict"] == "APPROVE"
    assert len(meta["critical_issues"]) == 0
    assert len(meta["minor_suggestions"]) == 1
    assert meta["minor_suggestions"][0]["path"] == "src/main.py"
    assert meta["minor_suggestions"][0]["line"] == 25
    assert len(meta["risks"]) == 1
    assert meta["risks"][0]["risk"] == "High concurrency throughput"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_code_review_metadata_includes_verified_invariants() -> None:
    """Invariants rendered in the body must also survive into the metadata footer."""
    cr = CodeReviewResponse(
        executive_summary="Audit pass.",
        verdict="APPROVE",
        verified_invariants=[
            VerifiedInvariant(
                invariant="Signature check runs before dispatch",
                path="src/main.py",
                line=12,
                evidence="Covered by tests/unit/test_main.py::test_rejects_bad_signature",
            )
        ],
    )
    md = cr.to_markdown(verdict="APPROVE")
    assert "Signature check runs before dispatch" in md

    meta = extract_review_metadata(md)
    assert meta is not None
    assert meta["verified_invariants"] == [
        {
            "invariant": "Signature check runs before dispatch",
            "path": "src/main.py",
            "line": 12,
            "evidence": "Covered by tests/unit/test_main.py::test_rejects_bad_signature",
        }
    ]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_sync_review_metadata_includes_verified_invariants() -> None:
    """Sync reviews must serialize verified invariants the same way as initial reviews."""
    sync = SyncReviewResponse(
        summary="Update preserves the contract.",
        verdict="APPROVE",
        verified_invariants=[
            VerifiedInvariant(
                invariant="Retry cap is unchanged",
                path="src/worker.py",
                line=40,
                evidence="Constant is untouched in the incremental diff",
            )
        ],
    )
    meta = extract_review_metadata(sync.to_markdown(verdict="APPROVE"))
    assert meta is not None
    assert [i["invariant"] for i in meta["verified_invariants"]] == ["Retry cap is unchanged"]


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_sync_review_response_embeds_metadata() -> None:
    """Ensure SyncReviewResponse.to_markdown() embeds structured metadata."""
    sync = SyncReviewResponse(
        summary="PR update addresses critical finding.",
        verdict="APPROVE",
        resolutions=[
            SyncResolutionItem(
                item_description="Fixed memory leak in worker",
                status="RESOLVED",
                evidence="Verified in worker.py:45 diff",
                category="CRITICAL",
            )
        ],
        critical_issues=[],
        minor_suggestions=[],
    )
    md = sync.to_markdown(verdict="APPROVE")
    assert "<!-- hannibal-review-metadata:" in md
    assert "* **Update Summary:**" not in md
    assert "### 1. Synchronization Summary\n\nPR update addresses critical finding." in md

    meta = extract_review_metadata(md)
    assert meta is not None
    assert meta["version"] == 1
    assert meta["type"] == "sync"
    assert meta["verdict"] == "APPROVE"
    assert len(meta["resolutions"]) == 1
    assert meta["resolutions"][0]["status"] == "RESOLVED"
    assert meta["resolutions"][0]["item_description"] == "Fixed memory leak in worker"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_parse_legacy_review_findings_pr_229_autopsy() -> None:
    """Ensure legacy markdown parser finds critical findings even when other sections are None found."""
    # This was the exact structure of PR #229 Review 1 that broke naive substring checks
    legacy_body = """## 🛡️ Code Review: `REQUEST_CHANGES`

### 1. Executive Summary

* **Summary & Justification:** Multi-line replacement diff bombs detected.

---

### 2. Action Items

#### 🔴 Critical (Must Fix Before Merge)
* 🔴 **[Single-line replacement diff bombs on multi-line suggestions]** (File: `src/webhook_agent/comment_poster.py`, Line: 65)
  * *Description*: Replacement diff bombs on multi-line suggestions.
  * *Suggested Fix*: Implement multi-line start_line handling.

#### 🟡 Suggestions & Maintainability
* *None found.*

---

### 3. Potential Risks & Edge Cases

* *None identified for this PR scope.*
"""
    findings = parse_legacy_review_findings(legacy_body)
    assert len(findings["critical_issues"]) == 1
    assert "Single-line replacement diff bombs" in findings["critical_issues"][0]["description"]
    assert len(findings["minor_suggestions"]) == 0
    assert len(findings["risks"]) == 0

    assert has_actionable_findings(legacy_body, state="DISMISSED") is True

    actionable = get_actionable_findings(legacy_body)
    assert len(actionable) == 1
    assert actionable[0]["category"] == "CRITICAL"


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_has_actionable_findings_clean_pass() -> None:
    """Ensure clean pass review without findings returns False for has_actionable_findings."""
    clean_body = """## 🛡️ Code Review: `APPROVE`

### 1. Executive Summary

* **Summary & Justification:** Clean pass.

---

### 2. Action Items

#### 🔴 Critical (Must Fix Before Merge)
* *None found.*

#### 🟡 Suggestions & Maintainability
* *None found.*

---

### 3. Potential Risks & Edge Cases

* *None identified for this PR scope.*
"""
    assert has_actionable_findings(clean_body, state="APPROVED") is False
    assert len(get_actionable_findings(clean_body)) == 0


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_format_findings_for_agent() -> None:
    """Ensure format_findings_for_agent creates a clean, decoupled checklist."""
    findings = [
        {
            "category": "CRITICAL",
            "file_path": "src/auth.py",
            "line_number": 88,
            "description": "JWT expiry unchecked",
            "suggested_fix": "verify_exp(token)",
        },
        {
            "category": "RISK",
            "description": "Unbounded cache growth",
            "recommendation": "Set max_size=1000",
        },
    ]

    formatted = format_findings_for_agent(findings)
    assert "[CRITICAL] [File: src/auth.py:88] JWT expiry unchecked" in formatted
    assert "Suggested Fix: verify_exp(token)" in formatted
    assert "[RISK] Unbounded cache growth" in formatted
    assert "Recommendation: Set max_size=1000" in formatted
    # Must NOT contain HTML comments or Markdown badges
    assert "<!--" not in formatted
    assert "🛡️" not in formatted


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_enforce_verdict_preserves_resolutions_on_dismissed_prior_review_with_findings() -> None:
    """End-to-end regression: Verify that _enforce_verdict preserves resolutions

    when the prior review was auto-dismissed (state='DISMISSED') but had findings.
    This was the exact regression on PR #229 Review 3.
    """
    mock_pr = MagicMock()
    mock_rv_dismissed = MagicMock()
    mock_rv_dismissed.state = "DISMISSED"
    mock_rv_dismissed.user.login = "hannibal-hub-agents[bot]"
    # Body containing 1 critical item and "None found" / "None identified" in other sections
    mock_rv_dismissed.body = """## 🛡️ Code Review: `REQUEST_CHANGES`

### 2. Action Items
#### 🔴 Critical (Must Fix Before Merge)
* 🔴 **[Single-line replacement diff bombs]** (File: `src/webhook_agent/comment_poster.py`, Line: 65)
#### 🟡 Suggestions & Maintainability
* *None found.*
### 3. Potential Risks & Edge Cases
* *None identified for this PR scope.*
"""
    mock_pr.get_reviews.return_value = [mock_rv_dismissed]
    mock_pr.get_files.return_value = []

    json_input = """{
        "summary": "Resolved diff bomb issue with multi-line start_line ranges.",
        "resolutions": [
            {
                "item_description": "Single-line replacement diff bombs",
                "status": "RESOLVED",
                "evidence": "Verified multi-line anchor handling in comment_poster.py",
                "category": "CRITICAL"
            }
        ],
        "critical_issues": [],
        "minor_suggestions": []
    }"""
    rendered_md, verdict, _inline = _enforce_verdict(json_input, "APPROVE", pr=mock_pr)
    assert verdict == "APPROVE"
    # The resolution must NOT be cleared!
    assert "Single-line replacement diff bombs" in rendered_md
    assert "[RESOLVED]" in rendered_md
    assert "No prior review items tracked." not in rendered_md


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_agent_prompt_builder_decouples_markdown_and_feeds_structured_findings() -> None:
    """Ensure agent user message contains structured checklist and decouples from human Markdown."""
    from webhook_agent.core.agent_definition import WebhookAgent

    payload = {
        "canonical": "pull_request.synchronize",
        "event_name": "pull_request",
        "action": "synchronize",
        "raw_payload": {
            "pull_request": {"number": 100, "title": "Test PR", "body": "PR description"},
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "dev"},
            "prior_actionable_findings": [
                {
                    "category": "CRITICAL",
                    "file_path": "src/core.py",
                    "line_number": 42,
                    "description": "Unvalidated buffer write",
                    "suggested_fix": "check_bounds(len)",
                },
                {
                    "category": "SUGGESTION",
                    "file_path": "src/util.py",
                    "line_number": 15,
                    "description": "Simplify regex pattern",
                },
            ],
            "previous_bot_reviews": "## 🛡️ Code Review: `REQUEST_CHANGES`\n### 1. Executive Summary\n...",
        },
    }

    agent = WebhookAgent(dry_run=True)
    msg = agent._build_user_message(payload)
    user_msg = msg.parts[0].text
    # Must contain structured prior review checklist
    assert "### 📋 Pre-Fetched Prior Review Findings to Verify" in user_msg
    assert "[CRITICAL] [File: src/core.py:42] Unvalidated buffer write" in user_msg
    assert "[SUGGESTION] [File: src/util.py:15] Simplify regex pattern" in user_msg
    assert "Suggested Fix: check_bounds(len)" in user_msg
    assert "ACTION DIRECTIVE FOR RESOLUTION TRACKER" in user_msg

    # Must NOT contain raw human review markdown from previous_bot_reviews
    assert "## 🛡️ Code Review: `REQUEST_CHANGES`" not in user_msg
    assert "### 1. Executive Summary" not in user_msg


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_agent_prompt_builder_clean_pass_orders_empty_resolutions() -> None:
    """Ensure agent user message orders empty resolutions when prior review had 0 findings."""
    from webhook_agent.core.agent_definition import WebhookAgent

    payload = {
        "canonical": "pull_request.synchronize",
        "event_name": "pull_request",
        "action": "synchronize",
        "raw_payload": {
            "pull_request": {"number": 100, "title": "Test PR", "body": "PR description"},
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "dev"},
            "prior_actionable_findings": [],
            "prior_reviews_had_findings": False,
            "previous_bot_reviews": "## 🛡️ Code Review: `APPROVE`\n*None found.*",
        },
    }

    agent = WebhookAgent(dry_run=True)
    msg = agent._build_user_message(payload)
    user_msg = msg.parts[0].text
    assert "### 📋 Pre-Fetched Prior Review Status" in user_msg
    assert "Previous reviews on this PR identified 0 actionable" in user_msg
    assert "You MUST leave 'resolutions' as an empty list ([])" in user_msg
    assert "## 🛡️ Code Review:" not in user_msg


@pytest.mark.unit
@pytest.mark.webhook_agent
def test_prefetch_previous_bot_reviews_extracts_structured_metadata() -> None:
    """Ensure _prefetch_previous_bot_reviews in processor extracts structured findings from metadata."""
    from webhook_agent.processor import _prefetch_previous_bot_reviews

    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_rv = MagicMock()
    mock_rv.user.login = "hannibal-hub-agents[bot]"
    mock_rv.state = "COMMENT"
    mock_rv.body = (
        "## Review Body\n"
        "<!-- hannibal-review-metadata: "
        '{"version": 1, "type": "initial", "verdict": "REQUEST_CHANGES", '
        '"critical_issues": [{"path": "src/api.py", "line": 50, "description": "Auth bypass"}], '
        '"minor_suggestions": [], "risks": []} -->'
    )

    mock_pr.get_reviews.return_value = [mock_rv]
    mock_repo.get_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    payload = {
        "event_name": "pull_request",
        "action": "synchronize",
        "raw_payload": {
            "pull_request": {"number": 42},
            "repository": {"full_name": "owner/repo"},
        },
    }

    _prefetch_previous_bot_reviews(mock_gh, "owner/repo", payload)

    raw = payload["raw_payload"]
    assert raw["prior_reviews_had_findings"] is True
    assert len(raw["prior_actionable_findings"]) == 1
    assert raw["prior_actionable_findings"][0]["category"] == "CRITICAL"
    assert raw["prior_actionable_findings"][0]["description"] == "Auth bypass"
    assert raw["prior_actionable_findings"][0]["file_path"] == "src/api.py"
    assert raw["prior_actionable_findings"][0]["line_number"] == 50
