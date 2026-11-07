"""End-to-end integration tests for the translated Hunter-Critic pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

from webhook_agent.review.critic import CriticEngine, SeverityGovernor
from webhook_agent.review.hunter import DefectGrouper, HunterEngine, ReviewBudgeter
from webhook_agent.review.schemas import CandidateFinding, CodeReviewResponse, ScorecardItem
from webhook_agent.tools.github_tools import review


def test_hunter_critic_pipeline_e2e_grouping_and_critic_filtering() -> None:
    diff_text = """diff --git a/src/processor.py b/src/processor.py
index 1111111..2222222 100644
--- a/src/processor.py
+++ b/src/processor.py
@@ -10,6 +10,12 @@ def process():
+    x = unwrap_optional(val)
+    y = unwrap_optional(other)
+    z = unwrap_optional(third)
+    w = unwrap_optional(fourth)
+    return x + y + z + w
"""

    budgeter = ReviewBudgeter()
    # Reviewable churn is 5 lines -> budget (2, 3)
    low, high = budgeter.budget_for(5)
    assert (low, high) == (2, 3)

    # 4 candidate findings with same defect category (none_check)
    raw_args = ["val", "other", "third", "fourth"]
    raw_candidates = [
        {
            "id": f"cand-{i}",
            "file_path": "src/processor.py",
            "line_number": 10 + i,
            "severity": "CRITICAL",  # LLM claiming CRITICAL
            "confidence_score": 9,
            "title": f"Missing null check {i}",
            "description": f"Potential NoneType dereference when calling unwrap_optional at {i}",
            "window": f"{10 + i}│+    {['x', 'y', 'z', 'w'][i]} = unwrap_optional({raw_args[i]})",
            "verify_steps": "check local variable assignment",
        }
        for i in range(4)
    ]

    hunter = HunterEngine()
    parsed = hunter.parse_candidate_findings(raw_candidates, apply_grouping=True)

    # 3+ defect grouping collapses 4 findings into 1 consolidated finding
    assert len(parsed) == 1
    grouped = parsed[0]
    assert "4 instances observed" in grouped.title

    critic = CriticEngine(diff_text=diff_text)
    survivors = critic.filter_and_verify(parsed)
    assert len(survivors) == 1

    # Apply Severity Governor: unproven CRITICAL is governed to MAJOR
    governor = SeverityGovernor()
    governed_survivors = governor.apply_governance_to_issues(survivors)
    assert len(governed_survivors) == 1
    assert governed_survivors[0].severity == "MAJOR"


def test_review_tool_with_hunter_critic_governance() -> None:
    # Build a CodeReviewResponse with raw candidate findings
    response = CodeReviewResponse(
        verdict="COMMENT",
        executive_summary="Automated review with Hunter-Critic integration.",
        scorecard=[
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Test Coverage & Regressions", status="PASS", notes="Clean."),
        ],
        candidate_findings=[
            CandidateFinding(
                id="cand-1",
                file_path="src/worker.py",
                line_number=25,
                severity="CRITICAL",  # Model claims CRITICAL
                confidence_score=9,
                title="Potential race condition",
                description="Worker loop state read without lock",
                suggested_fix="with self._lock:\n    state = self._state",
                window="25│+    state = self._state",
                verify_steps="check thread safety in worker.py",
            )
        ],
        verified_invariants=[
            "src/worker.py:25: `state = self._state` assigned in worker loop",
        ],
        semantic_invariants=[],
    )

    mock_pr = MagicMock()
    mock_pr.state = "open"
    mock_pr.merged = False
    mock_pr.head.sha = "abcdef123456"

    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = mock_pr

    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    mock_ctx = MagicMock()
    mock_ctx.state = {
        "gh_client": mock_gh,
        "repo_full_name": "owner/repo",
        "formal_review_eligible": True,
        "skip_investigation_gate": True,
    }

    # Call review tool
    result = review(mock_ctx, pr_number=42, report=response)

    # Review is processed, candidate findings evaluated, and submitted to mock_pr
    assert "submitted" in result.lower() or "success" in result.lower()
    # Confirm that candidate was governed to MAJOR and populated into issues
    assert response.critical_issues or response.minor_suggestions
    all_issues = response.critical_issues + response.minor_suggestions
    assert any(i.severity == "MAJOR" for i in all_issues)
    assert not any(i.severity == "CRITICAL" for i in all_issues)
