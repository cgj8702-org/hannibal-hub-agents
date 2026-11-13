"""Unit tests for machine-proven invariant dominance, anchor snapping, and semantic grounding.

Verifies:
1. Anchor snapping within +/- 10 lines of diff hunk additions in is_machine_verified_invariant.
2. Rejection of line citations beyond the 10-line tolerance.
3. Automatic promotion of grounded semantic invariants to verified_invariants.
4. Dominance enforcement capping semantic invariants <= 2 and ensuring len(verified) >= len(semantic).
5. Active semantic fact-checking requiring operative invariant verbs and rejecting phantom bare symbols.
6. Review tool rejection when semantic invariants outnumber verified invariants on code PRs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from webhook_agent.review.grounding import (
    enforce_invariant_dominance,
    is_machine_verified_invariant,
    promote_semantic_to_verified,
    verify_semantic_invariant,
)
from webhook_agent.review.schemas import CodeReviewResponse, ScorecardItem

SAMPLE_DIFF = """--- a/src/worker.py
+++ b/src/worker.py
@@ -30,5 +30,12 @@ def run_loop():
     setup_logging()
+    touch_worker_heartbeat()
+    notify_systemd_watchdog()
+    if candidate.severity == "NIT":
+        return False
     process_events()
"""


def test_is_machine_verified_snapping_within_tolerance() -> None:
    """An invariant citing a line within 10 lines of hunk additions is machine-verified."""
    # Line 32 is exact hunk addition; line 25 is within 10 lines and references worker
    inv_exact = "Worker heartbeat touch verified in diff. (src/worker.py:32)"
    inv_snapped = "Worker heartbeat updates triggered synchronously. (src/worker.py:27)"

    assert is_machine_verified_invariant(inv_exact, SAMPLE_DIFF, ["src/worker.py"])
    assert is_machine_verified_invariant(inv_snapped, SAMPLE_DIFF, ["src/worker.py"])


def test_is_machine_verified_snapping_rejects_beyond_tolerance() -> None:
    """A citation more than 10 lines away from any diff hunk is rejected."""
    # Line 95 is 63 lines away from the hunk (lines 30-42)
    inv_far = "Worker heartbeat updates triggered synchronously. (src/worker.py:95)"
    assert not is_machine_verified_invariant(inv_far, SAMPLE_DIFF, ["src/worker.py"])


def test_promote_semantic_to_verified_elevates_grounded_citations() -> None:
    """promote_semantic_to_verified promotes diff-grounded assertions to verified_invariants."""
    semantic = [
        "Critic engine ignores 'NIT' severity findings automatically. (src/worker.py:33)",
        "Worker heartbeat updates are triggered synchronously. (src/worker.py:31)",
        "The system maintains overall cluster stability across restarts.",
    ]

    promoted, remaining = promote_semantic_to_verified(semantic, SAMPLE_DIFF, ["src/worker.py"])

    assert len(promoted) == 2
    assert "src/worker.py:33" in promoted[0]
    assert "src/worker.py:31" in promoted[1]
    assert remaining == ["The system maintains overall cluster stability across restarts."]


def test_enforce_invariant_dominance_caps_and_demotes_excess() -> None:
    """enforce_invariant_dominance caps semantic <= 2 and ensures verified >= semantic."""
    # Case 1: 3 verified, 4 semantic -> capped to 2 semantic, 2 excess demoted
    v1 = ["Verified 1", "Verified 2", "Verified 3"]
    s1 = ["Semantic 1", "Semantic 2", "Semantic 3", "Semantic 4"]
    ret_v, ret_s, demoted = enforce_invariant_dominance(v1, s1, max_semantic=2, is_code_pr=True)
    assert len(ret_v) == 3
    assert len(ret_s) == 2
    assert demoted == ["Semantic 3", "Semantic 4"]

    # Case 2: 1 verified, 3 semantic -> capped to 1 semantic (verified dominance), 2 demoted
    v2 = ["Verified 1"]
    s2 = ["Semantic 1", "Semantic 2", "Semantic 3"]
    ret_v2, ret_s2, demoted2 = enforce_invariant_dominance(v2, s2, max_semantic=2, is_code_pr=True)
    assert len(ret_v2) == 1
    assert len(ret_s2) == 1
    assert demoted2 == ["Semantic 2", "Semantic 3"]

    # Case 3: 0 verified, 2 semantic on code PR -> 0 semantic allowed, all demoted
    v3: list[str] = []
    s3 = ["Semantic 1", "Semantic 2"]
    ret_v3, ret_s3, demoted3 = enforce_invariant_dominance(v3, s3, max_semantic=2, is_code_pr=True)
    assert len(ret_v3) == 0
    assert len(ret_s3) == 0
    assert demoted3 == ["Semantic 1", "Semantic 2"]


def test_enforce_invariant_dominance_non_code_pr() -> None:
    """On non-code PRs, semantic invariants up to max_semantic are permitted without code proof."""
    v: list[str] = []
    s = ["Documentation outlines security boundaries.", "Architecture guide updated."]
    ret_v, ret_s, demoted = enforce_invariant_dominance(v, s, max_semantic=2, is_code_pr=False)
    assert len(ret_v) == 0
    assert len(ret_s) == 2
    assert demoted == []


def test_verify_semantic_invariant_requires_operative_verb() -> None:
    """verify_semantic_invariant demotes claims that lack an operative invariant verb."""
    valid_claim = "Ensures worker heartbeat updates survive event loop timeouts"
    status, _reason = verify_semantic_invariant(valid_claim, SAMPLE_DIFF, ["src/worker.py"])
    assert status == "GROUNDED"

    passive_commentary = "Worker heartbeat module implementation details"
    status_passive, reason_passive = verify_semantic_invariant(
        passive_commentary, SAMPLE_DIFF, ["src/worker.py"]
    )
    assert status_passive == "UNVERIFIED"
    assert "operative invariant verb" in reason_passive


def test_verify_semantic_invariant_accepts_architectural_verbs() -> None:
    """verify_semantic_invariant accepts architectural action verbs like scoped, eliminating, filtered, isolated."""
    claims = [
        "Attribute call site identification is strictly scoped to importing modules, eliminating noise",
        "Filtered candidate call sites are isolated from external modules",
        "Propagated base_sha ensures consistent baseline resolution",
    ]
    for claim in claims:
        status, reason = verify_semantic_invariant(claim, SAMPLE_DIFF, ["src/worker.py"])
        assert status == "GROUNDED", f"Claim failed grounding: {claim} -> {status} ({reason})"


def test_verify_semantic_invariant_rejects_phantom_bare_symbols() -> None:
    """verify_semantic_invariant detects and rejects unbackticked phantom snake_case symbols."""
    phantom_claim = "Ensures phantom_distributed_lock protects against duplicate sweeps"
    status, reason = verify_semantic_invariant(phantom_claim, SAMPLE_DIFF, ["src/worker.py"])
    assert status == "HALLUCINATED"
    assert "phantom_distributed_lock" in reason


def test_review_enforcer_dominance_integration() -> None:
    """review_enforcer applies promotion, partitioning, and dominance in review pipeline."""
    from webhook_agent.review.critic import CriticEngine
    from webhook_agent.review.review_enforcer import _finalize_enforced_review

    cr = CodeReviewResponse(
        executive_summary="Heartbeat integration test.",
        scorecard=[
            ScorecardItem(
                dimension="Logic & Boundaries",
                status="PASS",
                notes="Verified in src/worker.py:32",
            ),
            ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Test Coverage & Regressions", status="PASS", notes="Clean."),
        ],
        verified_invariants=[],
        semantic_invariants=[
            "Worker heartbeat touch verified in diff. (src/worker.py:32)",
            "Ensures system stability during high load.",
            "Maintains consistent logging across worker threads.",
            "Guarantees graceful shutdown on SIGINT.",
        ],
        critical_issues=[],
        minor_suggestions=[],
        unverified_claims=[],
    )

    pr_mock = MagicMock()
    pr_mock.labels = []
    _rendered_body, enforced_verdict, _inline = _finalize_enforced_review(
        cr,
        critic_engine=CriticEngine(),
        det_contract_item=None,
        det_security_item=None,
        det_test_item=None,
        diff_for_grounding=SAMPLE_DIFF,
        diff_text=SAMPLE_DIFF,
        changed_paths=["src/worker.py"],
        head_sha="head123",
        pr=pr_mock,
        pr_label="test#1",
        bot_reviews=[],
        existing_comments_data=[],
        review_budget=None,
        has_prior_reviews=False,
        is_intended_request_changes=False,
        is_body_comment=False,
        is_caller_comment=False,
    )

    # The diff-grounded invariant must be promoted to verified_invariants
    assert len(cr.verified_invariants) >= 1
    assert any("src/worker.py:32" in inv for inv in cr.verified_invariants)
    # semantic_invariants must be capped and dominated
    assert len(cr.semantic_invariants) <= len(cr.verified_invariants)
    assert len(cr.semantic_invariants) <= 2
    # Excess must be demoted to unverified_claims
    assert len(cr.unverified_claims) >= 1
    # Demoted unverified claims must clamp enforced_verdict to COMMENT
    assert enforced_verdict == "COMMENT"
    assert cr.verdict == "COMMENT"


def test_github_tools_review_rejects_semantic_dominance_violation() -> None:
    """github_tools:review rejects code PR reviews where semantic invariants outnumber verified."""
    from webhook_agent.tools.github_tools import review

    ctx = MagicMock()
    ctx.state = {
        "formal_review_eligible": True,
        "repo_name": "cgj8702-org/hannibal-hub-agents",
        "skip_investigation_gate": False,
        "deterministic_changed_files": ["src/worker.py"],
    }

    cr = CodeReviewResponse(
        executive_summary="Dominance violation test.",
        scorecard=[
            ScorecardItem(dimension="Logic & Boundaries", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Concurrency & State", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Security & Secrets", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Contract Integrity", status="PASS", notes="Clean."),
            ScorecardItem(dimension="Test Coverage & Regressions", status="PASS", notes="Clean."),
        ],
        verified_invariants=["Only one verified invariant (`src/worker.py:32`)"],
        semantic_invariants=[
            "Ensures system stability during high load.",
            "Maintains consistent logging across worker threads.",
            "Guarantees graceful shutdown on SIGINT.",
        ],
        critical_issues=[],
        minor_suggestions=[],
    )

    with (
        patch("webhook_agent.tools.github_tools._get_repo_full_name", return_value="org/repo"),
        patch("webhook_agent.tools.github_tools._get_gh_from_ctx") as mock_gh,
    ):
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_pr.merged = False
        mock_repo.get_pull.return_value = mock_pr
        mock_gh.return_value.get_repo.return_value = mock_repo

        result = review(ctx, pr_number=1, report=cr)

        assert "Review submission rejected" in result
        assert (
            "semantic_invariants (count 3) outnumbers machine-proven verified_invariants" in result
        )
