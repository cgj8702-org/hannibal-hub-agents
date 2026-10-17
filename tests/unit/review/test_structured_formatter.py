"""Unit tests for Pydantic schemas, mechanical verdict math, and deterministic Markdown rendering."""

import pytest

from webhook_agent.review.formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    normalize_code_review_dict,
    normalize_sync_review_dict,
    render_code_review_markdown,
    render_sync_review_markdown,
)
from webhook_agent.review.review_enforcer import _enforce_verdict
from webhook_agent.review.schemas import (
    CodeReviewResponse,
    IssueItem,
    RiskItem,
    SyncResolutionItem,
    SyncReviewResponse,
    is_unmitigated_breaking_risk,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


@pytest.fixture
def valid_code_review_pass() -> CodeReviewResponse:
    return CodeReviewResponse(
        executive_summary="Clean feature implementation with full unit test coverage.",
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


def test_calculate_strict_verdict_explicit_comment(valid_code_review_pass):
    valid_code_review_pass.verdict = "COMMENT"
    verdict = calculate_strict_verdict(valid_code_review_pass)
    assert verdict == "COMMENT"


def test_render_code_review_markdown(valid_code_review_pass):
    md = render_code_review_markdown(valid_code_review_pass)
    assert "## 🛡️ Code Review: `APPROVE`" in md
    assert "Clean feature implementation with full unit test coverage." in md
    assert "Auditor Confidence" not in md


def test_enforce_verdict_with_raw_json(valid_code_review_pass):
    json_str = valid_code_review_pass.model_dump_json()
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "APPROVE"
    assert "## 🛡️ Code Review: `APPROVE`" in rendered_md


def test_enforce_verdict_with_codeblock_json(valid_code_review_pass):
    valid_code_review_pass.critical_issues.append(
        IssueItem(
            path="src/main.py",
            line=10,
            description="Syntax error in main logic",
            suggested_fix="Fix syntax error",
        )
    )
    json_str = f"```json\n{valid_code_review_pass.model_dump_json()}\n```"
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_str, "APPROVE")
    assert verdict == "REQUEST_CHANGES"
    assert "## 🛡️ Code Review: `REQUEST_CHANGES`" in rendered_md


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
        critical_issues=[],
        minor_suggestions=[],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "APPROVE"

    md = render_sync_review_markdown(sync_resp, verdict)
    assert "## ⚡ Code Review Update: `APPROVE`" in md
    assert "✅ **[RESOLVED]**" in md


def test_enforce_verdict_with_loose_schema_drift_json():
    """Verify self-healing normalizer recovers from LLM schema drift."""
    loose_json = """{
      "executive_summary": "Pull Request #81 centralizes Google ADK Gemini model instantiations.",
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
    rendered_md, verdict, _inline_comments = _enforce_verdict(loose_json, "APPROVE")
    assert verdict == "APPROVE"
    assert "## 🛡️ Code Review: `APPROVE`" in rendered_md
    assert "Circular import risk" in rendered_md


def test_enforce_verdict_with_loose_sync_review_json():
    """Verify self-healing normalizer recovers from SyncReviewResponse schema drift."""
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
    rendered_md, verdict, _inline_comments = _enforce_verdict(loose_sync_json, "APPROVE")
    assert verdict == "APPROVE"
    assert "## ⚡ Code Review Update: `APPROVE`" in rendered_md
    assert "Asset Path Resolution Mismatch" in rendered_md
    assert "✅ **[RESOLVED]**" in rendered_md


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
    from webhook_agent.review.formatter import parse_text_review_to_dict

    text_review = """# Code Review Report

### 1. Executive Summary
* **Goal of the PR:** Add logging telemetry and update configuration logic.

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
    assert data["executive_summary"] == "Add logging telemetry and update configuration logic."
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


def test_normalize_code_review_dict_edge_cases():
    """Verify self-healing normalizer handles empty inputs, non-dict objects, and string lists."""
    assert normalize_code_review_dict({}) == {
        "executive_summary": "Autonomous PR code review report.",
        "scorecard": [],
        "risks_and_edge_cases": [],
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [],
        "semantic_invariants": [],
        "unverified_claims": [],
        "context_gaps": [],
    }

    raw = {
        "executive_summary": "Test summary",
        "critical_issues": ["Loose string critical issue"],
        "minor_suggestions": ["Loose string minor suggestion"],
        "risks_and_edge_cases": ["Loose string risk item"],
    }
    normalized = normalize_code_review_dict(raw)
    assert normalized["executive_summary"] == "Test summary"
    assert len(normalized["critical_issues"]) == 1
    assert normalized["critical_issues"][0]["description"] == "Loose string critical issue"
    assert len(normalized["minor_suggestions"]) == 1
    assert normalized["minor_suggestions"][0]["description"] == "Loose string minor suggestion"
    assert len(normalized["risks_and_edge_cases"]) == 1
    assert normalized["risks_and_edge_cases"][0]["risk"] == "Loose string risk item"


def test_enforce_verdict_with_markdown_prefix_and_json_codeblock():
    """Verify _enforce_verdict strips leading LLM markdown text and parses embedded JSON codeblocks."""
    mixed_input = """# 🛡️ Hannibal Hub Code Review Report: `APPROVE`

### 1. Executive Summary
* **Goal of the PR:** Consolidate rate limits directly.

```json
{
  "executive_summary": "Comprehensive code review of PR #208 which successfully consolidates rate limits.",
  "confidence": 5.0,
  "critical_issues": [],
  "minor_suggestions": [
    {
      "file": "dev/model_sync.py",
      "line": 128,
      "suggestion": "Ensure any lingering documentation references are cleaned up."
    }
  ],
  "risks_and_edge_cases": [
    {
      "risk": "Missing model entries or malformed JSON",
      "recommendation": "Robust try/except block wrapping JSON loading."
    }
  ],
  "context_gaps": []
}
```"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(mixed_input, "APPROVE")
    assert verdict == "APPROVE"
    assert "## 🛡️ Code Review: `APPROVE`" in rendered_md
    assert "Comprehensive code review of PR #208" in rendered_md
    assert "Missing model entries or malformed JSON" in rendered_md
    assert "Audit Dimensions Evaluation" not in rendered_md
    assert "```json" not in rendered_md


def test_enforce_verdict_with_malformed_json_inside_codeblock():
    """Verify _enforce_verdict safely handles invalid JSON syntax inside codeblocks and falls back cleanly."""
    malformed_input = """# Code Review Report

### 1. Executive Summary
* **Goal of the PR:** Add logging telemetry.

```json
{
  "executive_summary": "Malformed JSON missing closing quote,
  "confidence": 5
}
```"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(malformed_input, "APPROVE")
    assert verdict == "APPROVE"
    assert "## 🛡️ Code Review: `APPROVE`" in rendered_md
    assert "Add logging telemetry" in rendered_md


def test_enforce_verdict_with_sync_review_containing_critical_issues_key():
    """Verify _enforce_verdict correctly routes SyncReviewResponse JSON containing critical_issues to sync review rendering."""
    sync_input = """{
      "summary": "Added unit test for malformed JSON inside codeblocks.",
      "resolutions": [
        {
          "item_description": "Add test case for invalid syntax inside codeblocks",
          "status": "RESOLVED",
          "evidence": "adf7aa9"
        }
      ],
      "critical_issues": [],
      "minor_suggestions": [],
      "confidence": 5.0
    }"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(sync_input, "APPROVE")
    assert verdict == "APPROVE"
    assert "## ⚡ Code Review Update: `APPROVE`" in rendered_md
    assert "Added unit test for malformed JSON inside codeblocks." in rendered_md
    assert "Autonomous PR code review report." not in rendered_md


def test_parse_text_review_approval_bullets_not_critical():
    """Verify Issue #110 fix: text reviews with score/approval bullets under Critical do NOT create fake critical issues or force REQUEST_CHANGES."""
    text_review = """## 🛡️ Code Review: `APPROVE`

### 1. Executive Summary

* **Summary & Justification:** PR #104 implements comprehensive logging hygiene.
* **Auditor Confidence:** `5/5`

---

### 2. Action Items

#### 🔴 Critical (Must Fix Before Merge)
* codebase: 5/5
* codebase: APPROVE

#### 🟡 Suggestions & Maintainability
* None found.

---

### 3. Potential Risks & Edge Cases

* None identified for this PR scope.
"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(text_review, "APPROVE")
    assert verdict == "APPROVE"
    assert "Action Items" not in rendered_md
    assert "None found" not in rendered_md
    assert "Summary & Justification:** Summary & Justification:**" not in rendered_md


def test_normalize_strips_redundant_summary_label_prefix():
    from webhook_agent.review.formatter import (
        normalize_code_review_dict,
        normalize_sync_review_dict,
    )

    d1 = {
        "executive_summary": "Update Summary:** Successfully addressed all critical review feedback."
    }
    norm1 = normalize_code_review_dict(d1)
    assert norm1["executive_summary"] == "Successfully addressed all critical review feedback."

    d2 = {"summary": "**Update Summary:** Refactored worker background sweep."}
    norm2 = normalize_sync_review_dict(d2)
    assert norm2["summary"] == "Refactored worker background sweep."


def test_render_sync_review_markdown_fallback_when_no_prior_reviews():
    from webhook_agent.review.formatter import render_sync_review_markdown
    from webhook_agent.review.schemas import SyncReviewResponse

    sync_resp = SyncReviewResponse(
        summary="PR update introduced changes.",
        confidence=5,
        resolutions=[],
        critical_issues=[],
        minor_suggestions=[],
    )

    rendered = render_sync_review_markdown(sync_resp, verdict="APPROVE", has_prior_reviews=False)
    assert "## 🛡️ Code Review: `APPROVE`" in rendered
    assert "## ⚡ Code Review Update:" not in rendered


def test_verdict_override_safety_rejects_upgrade_of_caller_request_changes():
    """Verify that _enforce_verdict never upgrades an explicit REQUEST_CHANGES event to APPROVE."""
    json_payload = """{
        "verdict": "REQUEST_CHANGES",
        "executive_summary": "Dependabot PR drops ansicon environment marker.",
        "confidence": 5,
        "critical_issues": [],
        "minor_suggestions": [],
        "risks_and_edge_cases": [
            {"risk": "Dropping sys_platform marker breaks non-Windows platforms.", "recommendation": "Revert jinxed"}
        ]
    }"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_payload, "REQUEST_CHANGES")
    assert verdict == "REQUEST_CHANGES"
    assert "## 🛡️ Code Review: `REQUEST_CHANGES`" in rendered_md
    assert "Dropping sys_platform marker breaks non-Windows platforms." in rendered_md


def test_verdict_override_safety_rejects_upgrade_of_titled_request_changes():
    """Verify that Markdown with a REQUEST_CHANGES header is not upgraded to APPROVE."""
    text_review = """## 🛡️ Code Review: `REQUEST_CHANGES`

### 1. Executive Summary

* **Summary & Justification:** Dependabot PR bumps markdownify, but drops environment marker sys_platform == 'win32'.
* **Auditor Confidence:** `5/5`

---

### 2. Action Items

#### 🔴 Critical (Must Fix Before Merge)
* *None found.*

#### 🟡 Suggestions & Maintainability
* *None found.*

---

### 3. Potential Risks & Edge Cases

* **Risk:** Dropping `sys_platform == 'win32'` forces ansicon on Linux.
"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(text_review, "APPROVE")
    assert verdict == "REQUEST_CHANGES"
    assert "## 🛡️ Code Review: `REQUEST_CHANGES`" in rendered_md


def test_normalize_code_review_promotes_breaking_risks():
    """Verify normalize_code_review_dict promotes breaking change risks to critical_issues."""
    data = {
        "executive_summary": "Routine dependency update.",
        "confidence": 5,
        "critical_issues": [],
        "risks_and_edge_cases": [
            {
                "category": "breaking_change",
                "description": "Dropped sys_platform == 'win32' marker in uv.lock",
                "suggested_fix": "Restore marker in uv.lock",
            }
        ],
    }
    normalized = normalize_code_review_dict(data)
    assert len(normalized["critical_issues"]) == 1
    assert "Dropped sys_platform" in normalized["critical_issues"][0]["description"]
    assert normalized["critical_issues"][0]["suggested_fix"] == "Restore marker in uv.lock"


def test_calculate_strict_verdict_respects_explicit_verdict_and_advisory_risks():
    """Verify calculate_strict_verdict flags explicit REQUEST_CHANGES and treats risks as advisory."""
    review_explicit = CodeReviewResponse(
        verdict="REQUEST_CHANGES",
        executive_summary="Reviewer requested changes.",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[],
    )
    assert calculate_strict_verdict(review_explicit) == "REQUEST_CHANGES"

    # Advisory risks alone must NOT mechanically flip verdict to REQUEST_CHANGES
    review_risk = CodeReviewResponse(
        executive_summary="Dependency bump.",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[
            RiskItem(
                risk="Unintended modification dropping environment marker in uv.lock",
                recommendation="Revert lockfile",
            )
        ],
    )
    assert calculate_strict_verdict(review_risk) == "APPROVE"


def test_is_unmitigated_breaking_risk_deprecated():
    """Verify is_unmitigated_breaking_risk is deprecated and consistently returns False."""
    assert (
        is_unmitigated_breaking_risk(
            "Minor risk of upstream breaking changes between Google ADK v2.9.2 and v2.12.0; mitigated by test suite execution and CI validation."
        )
        is False
    )
    assert (
        is_unmitigated_breaking_risk("Introduces breaking change to public API interface") is False
    )
    assert is_unmitigated_breaking_risk("Lockfile corruption detected in uv.lock") is False
    assert (
        is_unmitigated_breaking_risk("Unintended modification dropping required dependencies")
        is False
    )
    assert (
        is_unmitigated_breaking_risk("Unauthorized modification of security configuration") is False
    )


def test_calculate_strict_verdict_advisory_risks_never_flip_verdict():
    """Verify calculate_strict_verdict does not mechanically flip APPROVE to REQUEST_CHANGES on advisory risks."""
    review_mitigated = CodeReviewResponse(
        executive_summary="Routine dependency bump.",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[
            RiskItem(
                risk="Minor risk of upstream breaking changes between Google ADK v2.9.2 and v2.12.0; mitigated by test suite execution and CI validation.",
                recommendation="",
            )
        ],
    )
    assert calculate_strict_verdict(review_mitigated) == "APPROVE"

    review_negated = CodeReviewResponse(
        executive_summary="Safe refactor.",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[
            RiskItem(
                risk="Verified no breaking changes across public symbols.",
                recommendation="None needed",
            )
        ],
    )
    assert calculate_strict_verdict(review_negated) == "APPROVE"

    # Even unmitigated prose risks remain advisory; blockers belong in critical_issues or scorecard
    review_unmitigated = CodeReviewResponse(
        executive_summary="API alteration.",
        confidence=5,
        critical_issues=[],
        minor_suggestions=[],
        risks_and_edge_cases=[
            RiskItem(
                risk="Breaking change in webhook payload schema without fallback handler.",
                recommendation="Restore fallback handler",
            )
        ],
    )
    assert calculate_strict_verdict(review_unmitigated) == "APPROVE"


def test_normalize_code_review_dict_does_not_promote_mitigated_breaking_risk():
    """Verify normalize_code_review_dict does not promote mitigated risks to critical_issues."""
    raw = {
        "executive_summary": "Clean dependency bump.",
        "critical_issues": [],
        "minor_suggestions": [],
        "risks_and_edge_cases": [
            {
                "path": "pyproject.toml",
                "line": 16,
                "risk": "Minor risk of upstream breaking changes; mitigated by automated tests.",
                "recommendation": "Monitor production runs",
            }
        ],
    }
    normalized = normalize_code_review_dict(raw)
    assert len(normalized["critical_issues"]) == 0
    assert len(normalized["risks_and_edge_cases"]) == 1


def test_calculate_sync_verdict_respects_explicit_verdict_and_unaddressed_summary():
    """Verify calculate_sync_verdict flags explicit REQUEST_CHANGES and unaddressed summary issues."""
    sync_explicit = SyncReviewResponse(
        verdict="REQUEST_CHANGES",
        summary="Changes remain unaddressed.",
        confidence=5,
        resolutions=[
            SyncResolutionItem(
                item_description="Drop of environment marker",
                status="RESOLVED",
                evidence="Verified",
            )
        ],
        critical_issues=[],
        minor_suggestions=[],
    )
    assert calculate_sync_verdict(sync_explicit) == "REQUEST_CHANGES"

    sync_unresolved = SyncReviewResponse(
        summary="Prior critical findings remain unaddressed.",
        confidence=5,
        resolutions=[
            SyncResolutionItem(
                item_description="Fix vulnerability in endpoint",
                status="UNRESOLVED",
                evidence="Still present",
                category="CRITICAL",
            )
        ],
        critical_issues=[],
        minor_suggestions=[],
    )
    assert calculate_sync_verdict(sync_unresolved) == "REQUEST_CHANGES"

    # Summary scraping is deprecated: summary text alone without unresolved items or explicit verdict approves
    sync_summary_only = SyncReviewResponse(
        summary="Prior critical findings remain unaddressed.",
        confidence=5,
        resolutions=[],
        critical_issues=[],
        minor_suggestions=[],
    )
    assert calculate_sync_verdict(sync_summary_only) == "APPROVE"

    # Test negated blocking keywords does NOT force REQUEST_CHANGES
    sync_negated = SyncReviewResponse(
        summary="Updated PR guidelines with no blocking action items.",
        confidence=5,
        resolutions=[],
        critical_issues=[],
        minor_suggestions=[],
    )
    assert calculate_sync_verdict(sync_negated) == "APPROVE"


def test_format_suggestion_body_with_code():
    from webhook_agent.review.comment_poster import format_suggestion_body

    body = format_suggestion_body(
        "Use contextlib.suppress here.",
        "with contextlib.suppress(ValueError):\n    x = int(val)",
    )
    assert "Use contextlib.suppress here." in body
    assert "```suggestion\nwith contextlib.suppress(ValueError):\n    x = int(val)\n```" in body


def test_format_suggestion_body_strips_existing_fences():
    from webhook_agent.review.comment_poster import format_suggestion_body

    body = format_suggestion_body("Replace loop", "```python\nfor i in items:\n    pass\n```")
    assert "```suggestion\nfor i in items:\n    pass\n```" in body
    assert "```python" not in body


def test_format_suggestion_body_without_code():
    from webhook_agent.review.comment_poster import format_suggestion_body

    body = format_suggestion_body("Consider refactoring this module.")
    assert body == "Consider refactoring this module."
    assert "```suggestion" not in body


def test_build_github_review_comments_anchoring():
    from webhook_agent.review.comment_poster import build_github_review_comments

    diff_text = (
        "diff --git a/src/logic.py b/src/logic.py\n"
        "--- a/src/logic.py\n"
        "+++ b/src/logic.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-old_val = 1\n"
        "+new_val = 2\n"
        "+new_val_2 = 3\n"
    )

    issues = [
        IssueItem(
            path="src/logic.py",
            line=11,
            description="Fix value assignment",
            suggested_fix="new_val = 42",
        ),
        IssueItem(
            path="src/logic.py",
            line=999,  # Out of diff hunk
            description="Out of hunk issue",
            suggested_fix="fix()",
        ),
        IssueItem(
            path="src/other.py",  # Not in diff
            line=5,
            description="Other file issue",
            suggested_fix="other()",
        ),
        IssueItem(
            path="src/logic.py",
            line=None,  # No line number
            description="Missing line issue",
            suggested_fix="no_line()",
        ),
    ]

    comments, anchored_keys = build_github_review_comments(issues, diff_text)
    assert len(comments) == 1
    assert comments[0]["path"] == "src/logic.py"
    assert comments[0]["line"] == 11
    assert comments[0]["side"] == "RIGHT"
    assert "```suggestion\nnew_val = 42\n```" in comments[0]["body"]
    assert "src/logic.py:11" in anchored_keys


def test_enforce_verdict_with_pr_generates_inline_comments():
    from unittest.mock import MagicMock

    mock_pr = MagicMock()
    mock_file = MagicMock()
    mock_file.filename = "src/foo.py"
    mock_file.patch = "@@ -5,2 +5,3 @@\n def old():\n+    return 42\n"
    mock_pr.get_files.return_value = [mock_file]

    json_input = """{
        "verdict": "REQUEST_CHANGES",
        "executive_summary": "Bug found in foo.",
        "critical_issues": [
            {
                "path": "src/foo.py",
                "line": 6,
                "description": "Return 100 instead",
                "suggested_fix": "    return 100"
            }
        ],
        "minor_suggestions": []
    }"""
    _rendered_md, verdict, inline_comments = _enforce_verdict(json_input, "APPROVE", pr=mock_pr)
    assert verdict == "REQUEST_CHANGES"
    assert len(inline_comments) == 1
    assert inline_comments[0]["path"] == "src/foo.py"
    assert inline_comments[0]["line"] == 6
    assert inline_comments[0]["side"] == "RIGHT"
    assert "```suggestion\n    return 100\n```" in inline_comments[0]["body"]
    assert "Return 100 instead" in inline_comments[0]["body"]


def test_enforce_verdict_sync_review_resolution_guard_clears_hallucinations():
    from unittest.mock import MagicMock

    mock_pr = MagicMock()
    mock_rv_approved = MagicMock()
    mock_rv_approved.state = "APPROVED"
    mock_rv_approved.user.login = "hannibal-hub-agents[bot]"
    mock_pr.get_reviews.return_value = [mock_rv_approved]
    mock_pr.get_files.return_value = []

    json_input = """{
        "summary": "Follow-up review with no prior changes requested.",
        "resolutions": [
            {
                "issue_summary": "Imaginary bug that was never reported",
                "status": "RESOLVED",
                "evidence": "Cleaned up"
            }
        ],
        "critical_issues": [],
        "minor_suggestions": []
    }"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_input, "APPROVE", pr=mock_pr)
    assert verdict == "APPROVE"
    assert "Imaginary bug that was never reported" not in rendered_md
    assert "Resolution Tracker" not in rendered_md or "| Imaginary bug" not in rendered_md


def test_enforce_verdict_sync_review_resolution_guard_preserves_legitimate_resolutions():
    from unittest.mock import MagicMock

    mock_pr = MagicMock()
    mock_rv_changes = MagicMock()
    mock_rv_changes.state = "CHANGES_REQUESTED"
    mock_rv_changes.user.login = "hannibal-hub-agents[bot]"
    mock_pr.get_reviews.return_value = [mock_rv_changes]
    mock_pr.get_files.return_value = []

    json_input = """{
        "summary": "Follow-up review resolving prior change request.",
        "resolutions": [
            {
                "item_description": "Real bug reported in prior review",
                "status": "RESOLVED",
                "evidence": "Fix verified"
            }
        ],
        "critical_issues": [],
        "minor_suggestions": []
    }"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_input, "APPROVE", pr=mock_pr)
    assert verdict == "APPROVE"
    assert "Real bug reported in prior review" in rendered_md


def test_enforce_verdict_removal_only_patch_grounds_resolution():
    from unittest.mock import MagicMock

    from webhook_agent.review.review_enforcer import _enforce_verdict

    mock_pr = MagicMock()
    mock_rv = MagicMock()
    mock_rv.state = "CHANGES_REQUESTED"
    mock_rv.user.login = "hannibal-hub-agents[bot]"
    mock_rv.body = "Please remove dangerous_call()"
    mock_pr.get_reviews.return_value = [mock_rv]

    mock_file = MagicMock()
    mock_file.filename = "src/danger.py"
    # Pure removal patch (no + lines, only - lines)
    mock_file.patch = "@@ -10,3 +10,1 @@\n-dangerous_call()\n-cleanup_old()\n existing_line\n"
    mock_pr.get_files.return_value = [mock_file]
    mock_pr.get_review_comments.return_value = []

    json_input = """{
        "summary": "Follow-up review resolving prior change request.",
        "resolutions": [
            {
                "item_description": "dangerous_call security risk in src/danger.py",
                "status": "RESOLVED",
                "evidence": "dangerous_call() removed from src/danger.py"
            }
        ],
        "critical_issues": [],
        "minor_suggestions": []
    }"""
    rendered_md, verdict, _inline_comments = _enforce_verdict(json_input, "APPROVE", pr=mock_pr)
    assert verdict == "APPROVE"
    assert "dangerous_call security risk" in rendered_md
    assert "RESOLVED" in rendered_md
    assert "UNRESOLVED" not in rendered_md


def test_reproduce_pr_155_mangled_formatting():
    from webhook_agent.review.review_enforcer import _enforce_verdict

    text_input = (
        "## 🛡️ Code Review: `REQUEST_CHANGES`\n\n"
        "### 1. Executive Summary\n\n"
        "* **Summary & Justification:** The PR introduces experimental metrics calculation and shell payload execution utilities with multiple critical security vulnerabilities.\n\n"
        "---\n\n"
        "### 2. Action Items\n\n"
        "#### 🔴 Critical (Must Fix Before Merge)\n"
        "* `src/webhook_agent/flawed_feature.py` (Line 9)\n"
        "* Hardcoded credential pattern (`DUMMY_PRODUCTION_API_KEY`) violates security best practices and risks accidental secret exposure.\n"
        "* \n"
        "* `src/webhook_agent/flawed_feature.py` (Line 15)\n"
        "* \n"
        "* `src/webhook_agent/flawed_feature.py` (Line 21)\n"
        "* Null dereference hazard if `items` is `None` (allowed by type annotation `dict[str, Any] | None`).\n"
        "* \n"
        "* `src/webhook_agent/flawed_feature.py` (Line 22)\n"
        "* Guaranteed `ZeroDivisionError` due to division by literal zero (`total / 0`).\n"
        "* \n\n"
        "#### 🟡 Suggestions & Maintainability\n"
        "* *None found.*\n\n"
        "---\n\n"
        "### 3. Potential Risks & Edge Cases\n\n"
        "* *None identified for this PR scope.*\n"
    )
    rendered_md, _verdict, _ = _enforce_verdict(text_input, "REQUEST_CHANGES")
    assert "* `codebase`: " not in rendered_md
    assert "* `src/webhook_agent/flawed_feature.py:9`" in rendered_md
    assert "* `src/webhook_agent/flawed_feature.py:21`" in rendered_md
    assert "* `src/webhook_agent/flawed_feature.py:22`" in rendered_md


def test_sync_review_three_tier_resolution_rendering():
    """Verify that multi-tier resolutions render with clear category subheadings."""
    sync_resp = SyncReviewResponse(
        summary="Addressed review feedback across all dimensions.",
        resolutions=[
            SyncResolutionItem(
                item_description="Null check missing in auth.py",
                status="RESOLVED",
                evidence="auth.py:L45 added guard statement",
                category="CRITICAL",
            ),
            SyncResolutionItem(
                item_description="Extract helper function for token parsing",
                status="RESOLVED",
                evidence="token.py:L12 extracted parse_token_claims",
                category="SUGGESTION",
            ),
            SyncResolutionItem(
                item_description="Concurrency race condition on cache dict",
                status="RESOLVED",
                evidence="proactive_service.py:L18 added threading.Lock",
                category="RISK",
            ),
        ],
        critical_issues=[],
        minor_suggestions=[],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "APPROVE"

    md = render_sync_review_markdown(sync_resp, verdict)
    assert "#### 🔴 Critical Issues" in md
    assert "#### 🟡 Suggestions & Maintainability" in md
    assert "#### 🛡️ Risks & Edge Cases" in md
    assert "Null check missing in auth.py" in md
    assert "Extract helper function for token parsing" in md
    assert "Concurrency race condition on cache dict" in md


def test_sync_review_unresolved_suggestion_does_not_block_approval():
    """Verify that an unresolved optional suggestion does not force REQUEST_CHANGES."""
    sync_resp = SyncReviewResponse(
        summary="Resolved critical bug, left optional style suggestion for later.",
        resolutions=[
            SyncResolutionItem(
                item_description="Crash on empty list",
                status="RESOLVED",
                evidence="Fixed with guard check",
                category="CRITICAL",
            ),
            SyncResolutionItem(
                item_description="Consider renaming variable x to count",
                status="UNRESOLVED",
                evidence="Deferred to future refactoring",
                category="SUGGESTION",
            ),
        ],
        critical_issues=[],
        minor_suggestions=[],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "APPROVE"


def test_sync_review_unresolved_critical_blocks_approval():
    """Verify that an unresolved critical issue forces REQUEST_CHANGES."""
    sync_resp = SyncReviewResponse(
        summary="Partial fixes applied.",
        resolutions=[
            SyncResolutionItem(
                item_description="Severe memory leak in connection pool",
                status="UNRESOLVED",
                evidence="Still unclosed",
                category="CRITICAL",
            ),
        ],
        critical_issues=[],
        minor_suggestions=[],
        confidence=5,
    )
    verdict = calculate_sync_verdict(sync_resp)
    assert verdict == "REQUEST_CHANGES"


def test_normalize_sync_review_dict_preserves_categories():
    """Verify that normalize_sync_review_dict coerces categories accurately."""
    raw_data = {
        "summary": "Sync update",
        "resolutions": [
            {
                "item_description": "Crit 1",
                "status": "resolved",
                "evidence": "Fixed",
                "category": "critical",
            },
            {
                "item_description": "Sugg 1",
                "status": "resolved",
                "evidence": "Refactored",
                "category": "maintainability_suggestion",
            },
            {
                "item_description": "Risk 1",
                "status": "resolved",
                "evidence": "Locked",
                "category": "edge_case_risk",
            },
        ],
    }
    normalized = normalize_sync_review_dict(raw_data)
    resolutions = normalized["resolutions"]
    assert resolutions[0]["category"] == "CRITICAL"
    assert resolutions[1]["category"] == "SUGGESTION"
    assert resolutions[2]["category"] == "RISK"


def test_normalize_code_review_dict_preserves_invariants():
    """Verify that normalize_code_review_dict preserves invariants with formatted citations."""
    raw_data = {
        "executive_summary": "Comprehensive tool input gate PR.",
        "critical_issues": [],
        "minor_suggestions": [],
        "verified_invariants": [
            {
                "path": "src/webhook_agent/core/callbacks.py",
                "line": 281,
                "evidence": "Universal Pydantic schema lookup and validation correctly intercepts raw tool arguments.",
            },
            {
                "path": "src/webhook_agent/tools/github_tools.py",
                "line": "556",
                "evidence": "Explicitly verifies pr.draft and pr.mergeable status prior to invoking pr.merge().",
            },
        ],
    }
    normalized = normalize_code_review_dict(raw_data)
    assert "verified_invariants" in normalized
    invariants = normalized["verified_invariants"]
    assert len(invariants) == 2
    assert "src/webhook_agent/core/callbacks.py:281" in invariants[0]
    assert "src/webhook_agent/tools/github_tools.py:556" in invariants[1]

    # Verify invariant section is rendered in clean markdown
    cr_obj = CodeReviewResponse.model_validate(normalized)
    assert hasattr(cr_obj, "verified_invariants")
    md = cr_obj.to_markdown("APPROVE")
    assert "Verified Invariants" in md
    assert "- [x]" in md


def test_normalize_code_review_dict_preserves_string_invariants():
    """Verify that normalize_code_review_dict preserves plain string invariants."""
    raw_data = {
        "executive_summary": "Clean review.",
        "verified_invariants": ["In-memory cache expiration invariant preserved across threads."],
    }
    normalized = normalize_code_review_dict(raw_data)
    assert "verified_invariants" in normalized
    assert normalized["verified_invariants"] == [
        "In-memory cache expiration invariant preserved across threads."
    ]

    cr_obj = CodeReviewResponse.model_validate(normalized)
    assert cr_obj.verified_invariants == [
        "In-memory cache expiration invariant preserved across threads."
    ]
    md = cr_obj.to_markdown("APPROVE")
    assert "Verified Invariants" in md
    assert "- [x] In-memory cache expiration invariant preserved across threads." in md


def test_normalize_sync_review_dict_preserves_invariants():
    """Verify that normalize_sync_review_dict preserves invariants in synchronization reviews."""
    raw_data = {
        "summary": "Incremental sync diff verified.",
        "resolutions": [],
        "verified_invariants": [
            {
                "path": "src/webhook_agent/state/review_checkpoint.py",
                "line": 416,
                "evidence": "Checkpoint marked completed only after successful review writeback.",
            }
        ],
    }
    normalized = normalize_sync_review_dict(raw_data)
    assert "verified_invariants" in normalized

    sync_obj = SyncReviewResponse.model_validate(normalized)
    assert len(sync_obj.verified_invariants) == 1
    assert "src/webhook_agent/state/review_checkpoint.py:416" in sync_obj.verified_invariants[0]
    md = sync_obj.to_markdown("APPROVE")
    assert "Verified Invariants" in md
    assert "- [x]" in md


def test_audit_dimension_scorecard_rendering():
    """Verify CodeReviewResponse renders Audit Dimension Scorecard table correctly."""
    from webhook_agent.review.schemas import ScorecardItem

    cr = CodeReviewResponse(
        executive_summary="Sound PR implementation.",
        scorecard=[
            ScorecardItem(
                dimension="Logic & Boundaries",
                status="PASS",
                notes="Off-by-one errors guarded by boundary check.",
            ),
            ScorecardItem(
                dimension="Concurrency & State",
                status="PASS",
                notes="Locks held across state mutation.",
            ),
            ScorecardItem(
                dimension="Security & Secrets",
                status="PASS",
                notes="No credentials in code.",
            ),
            ScorecardItem(
                dimension="Contract Integrity",
                status="PASS",
                notes="Signatures backward compatible.",
            ),
            ScorecardItem(
                dimension="Test Coverage & Regressions",
                status="PASS",
                notes="Full unit test coverage added.",
            ),
        ],
        verified_invariants=[
            "Rate limiter fast-fail executes without thread blocking.",
        ],
    )
    md = cr.to_markdown(verdict="APPROVE")
    assert "### 2. Audit Dimension Scorecard" in md
    assert (
        "| **Logic & Boundaries** | `✅ PASS` | Off-by-one errors guarded by boundary check. |"
        in md
    )
    assert "| **Concurrency & State** | `✅ PASS` | Locks held across state mutation. |" in md
    assert "### 3. Verified Invariants" in md
    assert "- [x] Rate limiter fast-fail executes without thread blocking." in md


def test_scorecard_fail_triggers_request_changes():
    """Verify that a failing dimension on scorecard causes mechanical REQUEST_CHANGES."""
    from webhook_agent.review.schemas import ScorecardItem

    cr = CodeReviewResponse(
        executive_summary="PR has broken contract.",
        scorecard=[
            ScorecardItem(
                dimension="Contract Integrity",
                status="FAIL",
                notes="Breaking signature change without caller migration.",
            )
        ],
    )
    verdict = calculate_strict_verdict(cr)
    assert verdict == "REQUEST_CHANGES"


def test_scorecard_unknown_and_error_statuses() -> None:
    """Verify ScorecardItem handles UNKNOWN and ERROR without fail-open coercion to PASS."""
    from webhook_agent.review.schemas import ScorecardItem

    # Empty status defaults to UNKNOWN, not PASS
    item_empty = ScorecardItem(dimension="Security & Secrets", status="")
    assert item_empty.status == "UNKNOWN"
    assert "`❓ UNKNOWN`" in item_empty.to_table_row()

    # Unrecognized status defaults to UNKNOWN, not PASS
    item_unrec = ScorecardItem(dimension="Test Coverage & Regressions", status="some_random_text")
    assert item_unrec.status == "UNKNOWN"
    assert "`❓ UNKNOWN`" in item_unrec.to_table_row()

    # Explicit ERROR status renders ERROR badge
    item_err = ScorecardItem(
        dimension="Contract Integrity", status="ERROR", notes="Analyzer crash."
    )
    assert item_err.status == "ERROR"
    assert "`💥 ERROR`" in item_err.to_table_row()

    # Exception keyword sanitizes to ERROR
    item_crash = ScorecardItem(dimension="Logic & Boundaries", status="crash_in_ast")
    assert item_crash.status == "ERROR"


def test_scorecard_unknown_verdict_behavior() -> None:
    """Verify UNKNOWN/ERROR in scorecard does not hard block or silently approve."""
    from webhook_agent.review.schemas import ScorecardItem

    cr = CodeReviewResponse(
        executive_summary="Clean PR with unverified dimension.",
        scorecard=[
            ScorecardItem(
                dimension="Contract Integrity", status="UNKNOWN", notes="Analyzer timed out."
            ),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="No secrets."),
        ],
    )
    # UNKNOWN alone does not force REQUEST_CHANGES (only FAIL does)
    verdict = calculate_strict_verdict(cr)
    assert verdict == "APPROVE"


def test_parse_text_review_scorecard_unknown_and_error() -> None:
    """Verify Markdown text parser accurately parses UNKNOWN and ERROR without coercing to PASS."""
    from webhook_agent.review.formatter import parse_text_review_to_dict

    review_text = (
        "## 🛡️ Code Review: `COMMENT`\n\n"
        "### 1. Executive Summary\nAnalysis summary.\n\n"
        "### 2. Audit Dimension Scorecard\n\n"
        "| Dimension | Status | Notes |\n"
        "| :--- | :---: | :--- |\n"
        "| **Logic & Boundaries** | `❓ UNKNOWN` | Could not verify dynamic calls. |\n"
        "| **Contract Integrity** | `💥 ERROR` | Syntax error in file parser. |\n"
        "| **Concurrency & State** | `💥 EXCEPTION` | AST parser raised an unhandled exception. |\n"
        "| **Security & Secrets** | `✅ PASS` | Verified clean. |\n"
    )
    parsed = parse_text_review_to_dict(review_text)
    scorecard = parsed.get("scorecard", [])
    assert len(scorecard) == 4
    assert scorecard[0]["status"] == "UNKNOWN"
    assert scorecard[1]["status"] == "ERROR"
    assert scorecard[2]["status"] == "ERROR"
    assert scorecard[3]["status"] == "PASS"


def test_response_normalizers_scorecard_missing_status_defaults_to_unknown() -> None:
    """Verify normalize_code_review_dict and normalize_sync_review_dict default missing status to UNKNOWN."""
    from webhook_agent.review.formatter import (
        normalize_code_review_dict,
        normalize_sync_review_dict,
    )

    raw_cr = {
        "executive_summary": "Test summary",
        "scorecard": [
            {"dimension": "Logic & Boundaries", "status": "", "notes": "No status provided."},
            {"dimension": "Security & Secrets", "notes": "Status key omitted entirely."},
            "Contract Integrity: Bare string without status.",
        ],
    }
    norm_cr = normalize_code_review_dict(raw_cr)
    sc_cr = norm_cr.get("scorecard", [])
    assert len(sc_cr) == 3
    assert sc_cr[0]["status"] == "UNKNOWN"
    assert sc_cr[1]["status"] == "UNKNOWN"
    assert sc_cr[2]["status"] == "UNKNOWN"

    raw_sync = {
        "summary": "Sync summary",
        "scorecard": {
            "Concurrency & State": {"notes": "Dict format with missing status."},
            "Test Coverage & Regressions": "String note only in dict mapping",
        },
    }
    norm_sync = normalize_sync_review_dict(raw_sync)
    sc_sync = norm_sync.get("scorecard", [])
    assert len(sc_sync) == 2
    assert sc_sync[0]["status"] == "UNKNOWN"
    assert sc_sync[1]["status"] == "UNKNOWN"


def test_string_findings_get_no_placeholder_path() -> None:
    """Bare-string findings keep their text but never receive an invented file path."""
    code = normalize_code_review_dict(
        {
            "executive_summary": "s",
            "critical_issues": ["Race in cache refresh"],
            "minor_suggestions": ["Rename helper"],
        }
    )
    sync = normalize_sync_review_dict(
        {
            "summary": "s",
            "critical_issues": ["Race in cache refresh"],
            "minor_suggestions": ["Rename helper"],
        }
    )
    for normalized in (code, sync):
        for item in normalized["critical_issues"] + normalized["minor_suggestions"]:
            assert item["path"] == ""
            assert item["line"] is None


def test_risk_promotion_does_not_guess_a_file_path() -> None:
    """A risk without a valid file path and line number must not be promoted to critical_issues."""
    normalized = normalize_code_review_dict(
        {
            "executive_summary": "s",
            "risks_and_edge_cases": [
                {
                    "risk": "Lock marker change causes unauthorized modification of deps",
                    "recommendation": "Regenerate the lockfile",
                }
            ],
        }
    )
    promoted = normalized["critical_issues"]
    assert not promoted, "expected breaking risk without citation not to fabricate a critical issue"


def test_issue_without_path_does_not_render_empty_or_fake_location() -> None:
    from webhook_agent.review.schemas import IssueItem

    md = IssueItem(path="", description="Needs a real citation").to_markdown()
    assert "``" not in md
    assert "codebase" not in md
    assert "no file cited" in md
