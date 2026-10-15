"""Unit tests for CandidateFinding and VerificationVerdict schemas."""

from __future__ import annotations

import pytest

from webhook_agent.review.schemas import CandidateFinding, IssueItem, VerificationVerdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_candidate_finding_instantiation_and_defaults() -> None:
    finding = CandidateFinding(
        file_path="src/webhook_agent/worker.py",
        title="Uncaught exception in event loop",
        description="Event loop might raise unhandled TimeoutError during shutdown.",
    )
    assert finding.file_path == "src/webhook_agent/worker.py"
    assert finding.line_number == 1
    assert finding.start_line is None
    assert finding.severity == "MEDIUM"
    assert finding.confidence_score == 5
    assert finding.title == "Uncaught exception in event loop"
    assert finding.falsification_check == ""
    assert finding.suggested_fix is None


def test_candidate_finding_to_issue_item() -> None:
    finding = CandidateFinding(
        id="cand-1",
        file_path="src/webhook_agent/worker.py",
        line_number=42,
        start_line=40,
        severity="HIGH",
        confidence_score=9,
        title="Resource leak",
        description="SubscriberClient not closed on exit.",
        suggested_fix="client.close()",
        falsification_check="Check if a context manager or __del__ handles cleanup.",
    )
    issue = finding.to_issue_item()
    assert isinstance(issue, IssueItem)
    assert issue.path == "src/webhook_agent/worker.py"
    assert issue.line == 42
    assert issue.start_line == 40
    assert issue.description == "Resource leak: SubscriberClient not closed on exit."
    assert issue.suggested_fix == "client.close()"


def test_candidate_finding_to_issue_item_no_title() -> None:
    finding = CandidateFinding(
        file_path="foo.py",
        title="",
        description="Just description here.",
    )
    issue = finding.to_issue_item()
    assert issue.description == "Just description here."


def test_candidate_finding_sanitization() -> None:
    finding = CandidateFinding(
        file_path="  src/app.py \n",
        title="  Fix leak \t",
        description="  Detail \r\n",
        falsification_check="  Check ctx  ",
    )
    assert finding.file_path == "src/app.py"
    assert finding.title == "Fix leak"
    assert finding.description == "Detail"
    assert finding.falsification_check == "Check ctx"


def test_verification_verdict_schema() -> None:
    verdict = VerificationVerdict(
        finding_id="cand-1",
        verified=True,
        confidence_score=9,
        revised_description="Refined flaw explanation",
        revised_suggested_fix="improved_fix()",
    )
    assert verdict.finding_id == "cand-1"
    assert verdict.verified is True
    assert verdict.confidence_score == 9
    assert verdict.disproof_reason is None
    assert verdict.revised_description == "Refined flaw explanation"
    assert verdict.revised_suggested_fix == "improved_fix()"


def test_verification_verdict_disproved() -> None:
    verdict = VerificationVerdict(
        finding_id="cand-2",
        verified=False,
        confidence_score=2,
        disproof_reason="The caller already handles TimeoutError with exponential backoff.",
    )
    assert verdict.verified is False
    assert verdict.confidence_score == 2
    assert (
        verdict.disproof_reason
        == "The caller already handles TimeoutError with exponential backoff."
    )
