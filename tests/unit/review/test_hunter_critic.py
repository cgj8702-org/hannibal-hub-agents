"""Unit tests for Hunter and Critic competence review engines."""

from __future__ import annotations

import pytest

from webhook_agent.review.critic import CriticEngine
from webhook_agent.review.hunter import HunterEngine
from webhook_agent.review.schemas import CandidateFinding, IssueItem, VerificationVerdict

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_hunter_parse_candidate_findings() -> None:
    hunter = HunterEngine()
    raw = [
        {
            "id": "cand-1",
            "file_path": "src/app.py",
            "line_number": 10,
            "severity": "high",
            "confidence_score": 9,
            "title": "Unclosed resource",
            "description": "Session is not closed.",
            "suggested_fix": "session.close()",
            "falsification_check": "Verify if session has auto-cleanup context.",
        },
        {
            # Test fallback keys and default confidence
            "path": "src/utils.py",
            "line": 25,
            "severity": "nit",
            "description": "Variable naming could be clearer.",
        },
        # Already a CandidateFinding
        CandidateFinding(
            id="cand-3",
            file_path="src/main.py",
            line_number=1,
            title="Syntax error",
            description="Missing colon",
        ),
        # Non-dict / invalid item
        "invalid_item_string",
    ]

    parsed = hunter.parse_candidate_findings(raw)
    assert len(parsed) == 3

    assert parsed[0].id == "cand-1"
    assert parsed[0].file_path == "src/app.py"
    assert parsed[0].severity == "HIGH"
    assert parsed[0].confidence_score == 9
    assert parsed[0].suggested_fix == "session.close()"

    assert parsed[1].id == "candidate-2"
    assert parsed[1].file_path == "src/utils.py"
    assert parsed[1].line_number == 25
    assert parsed[1].severity == "NIT"
    assert parsed[1].confidence_score == 5

    assert parsed[2].id == "cand-3"
    assert parsed[2].file_path == "src/main.py"


def test_critic_evaluate_candidate_drops_nit() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="NIT",
        confidence_score=9,
        title="Style nit",
        description="Extra blank line",
    )
    assert critic.evaluate_candidate(cand) is False


def test_critic_evaluate_candidate_drops_low_confidence() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="MEDIUM",
        confidence_score=7,
        title="Possible race condition",
        description="Check lock acquisition",
    )
    assert critic.evaluate_candidate(cand) is False


def test_critic_evaluate_candidate_accepts_high_confidence() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        file_path="foo.py",
        severity="CRITICAL",
        confidence_score=8,
        title="SQL injection",
        description="Unsanitized query string",
    )
    assert critic.evaluate_candidate(cand) is True


def test_critic_evaluate_candidate_with_verdict() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        id="c-1",
        file_path="foo.py",
        severity="HIGH",
        confidence_score=9,
        title="Uncaught error",
        description="Might throw KeyError",
    )

    # Disproven verdict
    disproven = VerificationVerdict(
        finding_id="c-1",
        verified=False,
        confidence_score=2,
        disproof_reason="Outer dict has default key handler.",
    )
    assert critic.evaluate_candidate(cand, verdict=disproven) is False

    # Verified but low confidence verdict
    low_conf = VerificationVerdict(
        finding_id="c-1",
        verified=True,
        confidence_score=7,
    )
    assert critic.evaluate_candidate(cand, verdict=low_conf) is False

    # Verified high confidence verdict
    verified = VerificationVerdict(
        finding_id="c-1",
        verified=True,
        confidence_score=9,
    )
    assert critic.evaluate_candidate(cand, verdict=verified) is True


def test_critic_filter_and_verify() -> None:
    critic = CriticEngine(min_confidence=8)
    candidates = [
        CandidateFinding(
            id="c-1",
            file_path="src/a.py",
            line_number=10,
            severity="HIGH",
            confidence_score=9,
            title="Leaked connection",
            description="Old description",
            suggested_fix="conn.close()",
        ),
        CandidateFinding(
            id="c-2",
            file_path="src/b.py",
            line_number=20,
            severity="NIT",
            confidence_score=10,
            title="Formatting",
            description="Spacing",
        ),
        CandidateFinding(
            id="c-3",
            file_path="src/c.py",
            line_number=30,
            severity="MEDIUM",
            confidence_score=5,
            title="Speculative bug",
            description="Maybe null",
        ),
    ]

    verdicts = {
        "c-1": VerificationVerdict(
            finding_id="c-1",
            verified=True,
            confidence_score=9,
            revised_description="Refined description: connection leak on timeout",
            revised_suggested_fix="try:\n    yield\nfinally:\n    conn.close()",
        )
    }

    survivors = critic.filter_and_verify(candidates, verdicts=verdicts)
    assert len(survivors) == 1
    issue = survivors[0]
    assert isinstance(issue, IssueItem)
    assert issue.path == "src/a.py"
    assert issue.line == 10
    assert "Refined description: connection leak on timeout" in issue.description
    assert issue.suggested_fix == "try:\n    yield\nfinally:\n    conn.close()"


def test_critic_filter_issues_drops_nits_and_low_confidence() -> None:
    critic = CriticEngine(min_confidence=8)
    issues = [
        IssueItem(
            path="src/a.py",
            line=10,
            description="[NIT] Remove extra blank line",
            severity="NIT",
            confidence_score=9,
        ),
        IssueItem(
            path="src/b.py",
            line=20,
            description="Style nit: consider renaming variable",
            category="NIT",
            confidence_score=10,
        ),
        IssueItem(
            path="src/c.py",
            line=30,
            description="Nit: use list comprehension",
            confidence_score=8,
        ),
        IssueItem(
            path="src/d.py",
            line=40,
            description="Speculative race condition",
            severity="MEDIUM",
            confidence_score=7,
        ),
        IssueItem(
            path="src/e.py",
            line=50,
            description="Genuine unclosed file descriptor",
            severity="HIGH",
            confidence_score=9,
            suggested_fix="file.close()",
        ),
    ]

    filtered = critic.filter_issues(issues)
    assert len(filtered) == 1
    assert filtered[0].path == "src/e.py"
    assert "Genuine unclosed file descriptor" in filtered[0].description


def test_review_enforcer_critic_integration_drops_nits_and_upgrades_verdict() -> None:
    """Verify that _enforce_verdict filters out nit critical issues and upgrades verdict."""
    from webhook_agent.review.review_enforcer import _enforce_verdict

    # Raw payload has 1 critical issue that is a NIT, plus 2 verified invariants
    payload = {
        "verdict": "APPROVE",
        "executive_summary": "Autonomous PR code review report.",
        "critical_issues": [
            {
                "path": "src/app.py",
                "line": 15,
                "description": "[NIT] Please add trailing comma",
                "suggested_fix": ",",
            }
        ],
        "minor_suggestions": [],
        "verified_invariants": [
            {
                "invariant": "API client parses error responses cleanly",
                "path": "src/client.py",
                "line": 42,
                "evidence": "Inspected error response handler",
            },
            {
                "invariant": "DB connection timeout is bounded",
                "path": "src/db.py",
                "line": 88,
                "evidence": "Inspected pool configuration timeout",
            },
        ],
    }

    import json

    body_text = json.dumps(payload)
    rendered_body, enforced_verdict, inline_comments = _enforce_verdict(
        body_text, event="APPROVE", pr=None, review_mode="initial"
    )

    # Critic drops the NIT issue; with 0 critical issues and 2 invariants, verdict upgrades to APPROVE
    assert enforced_verdict == "APPROVE"
    assert (
        "*None found.*" in rendered_body
        or "Critical (Must Fix Before Merge)" not in rendered_body
        or "Trailing comma" not in rendered_body
    )
    assert inline_comments == []


def test_review_enforcer_candidate_findings_extraction() -> None:
    """Verify that _enforce_verdict processes raw candidate_findings through Hunter and Critic."""
    from webhook_agent.review.review_enforcer import _enforce_verdict

    payload = {
        "verdict": "REQUEST_CHANGES",
        "executive_summary": "Candidate findings review report.",
        "candidate_findings": [
            {
                "id": "cand-1",
                "file_path": "src/server.py",
                "line_number": 100,
                "severity": "CRITICAL",
                "confidence_score": 9,
                "title": "SQL Injection vulnerability",
                "description": "Unescaped user input passed to raw execute call.",
                "suggested_fix": "db.execute(query, (user_input,))",
                "falsification_check": "Check if user_input is pre-sanitized.",
            },
            {
                "id": "cand-2",
                "file_path": "src/utils.py",
                "line_number": 20,
                "severity": "NIT",
                "confidence_score": 10,
                "title": "Spacing nit",
                "description": "Double spaces in docstring",
            },
            {
                "id": "cand-3",
                "file_path": "src/routes.py",
                "line_number": 50,
                "severity": "MEDIUM",
                "confidence_score": 6,
                "title": "Low confidence edge case",
                "description": "Possible None return",
            },
        ],
        "verified_invariants": [
            {
                "invariant": "Inv1",
                "path": "src/a.py",
                "line": 1,
                "evidence": "Ev1",
            },
            {
                "invariant": "Inv2",
                "path": "src/b.py",
                "line": 2,
                "evidence": "Ev2",
            },
        ],
    }

    import json

    body_text = json.dumps(payload)
    rendered_body, enforced_verdict, inline_comments = _enforce_verdict(
        body_text, event="REQUEST_CHANGES", pr=None, review_mode="initial"
    )

    # cand-1 (CRITICAL, score 9) survives into critical_issues, enforcing REQUEST_CHANGES
    assert enforced_verdict == "REQUEST_CHANGES"
    assert "SQL Injection vulnerability" in rendered_body
    # cand-2 (NIT) and cand-3 (score 6) must not appear
    assert "Spacing nit" not in rendered_body
    assert "Low confidence edge case" not in rendered_body
    assert inline_comments == []


def test_issue_item_confidence_alias_and_critic_filter() -> None:
    """Verify IssueItem coerces 'confidence' to 'confidence_score' and CriticEngine filters it."""
    # Coercion from alias
    item_low = IssueItem.model_validate(
        {
            "path": "src/app.py",
            "line": 15,
            "description": "Possible latency spike",
            "confidence": 6,
        }
    )
    assert item_low.confidence_score == 6

    item_high = IssueItem.model_validate(
        {
            "path": "src/app.py",
            "line": 20,
            "description": "Hard crash on None input",
            "confidence": 9,
        }
    )
    assert item_high.confidence_score == 9

    critic = CriticEngine(min_confidence=8)
    survivors = critic.filter_issues([item_low, item_high])
    assert len(survivors) == 1
    assert survivors[0].line == 20
    assert survivors[0].description == "Hard crash on None input"


def test_critic_structured_telemetry_logging(caplog: pytest.LogCaptureFixture) -> None:
    """Verify CriticEngine logs structured telemetry with extra['json_fields']."""
    import logging

    critic = CriticEngine(min_confidence=8)
    cand_nit = CandidateFinding(
        id="cand-nit-1",
        file_path="src/foo.py",
        severity="NIT",
        confidence_score=9,
        title="Extra line",
        description="Just a nit",
    )
    cand_low = CandidateFinding(
        id="cand-low-1",
        file_path="src/foo.py",
        severity="MEDIUM",
        confidence_score=5,
        title="Minor edge",
        description="Low confidence finding",
    )

    with caplog.at_level(logging.INFO):
        assert critic.evaluate_candidate(cand_nit) is False
        assert critic.evaluate_candidate(cand_low) is False

    nit_records = [
        r
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("event") == "finding_falsified"
        and getattr(r, "json_fields", {}).get("candidate_id") == "cand-nit-1"
    ]
    assert len(nit_records) == 1
    assert nit_records[0].json_fields["category"] == "critic_drop"
    assert nit_records[0].json_fields["severity"] == "NIT"

    low_records = [
        r
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("event") == "finding_falsified"
        and getattr(r, "json_fields", {}).get("candidate_id") == "cand-low-1"
    ]
    assert len(low_records) == 1
    assert low_records[0].json_fields["category"] == "critic_drop"
    assert low_records[0].json_fields["confidence"] == 5


def test_review_tool_with_candidate_findings() -> None:
    """Verify review() tool in github_tools processes candidate_findings through Hunter & Critic."""
    from unittest.mock import MagicMock

    from webhook_agent.review.schemas import CodeReviewResponse
    from webhook_agent.tools.github_tools import review

    ctx = MagicMock()
    mock_gh = MagicMock()
    mock_repo = mock_gh.get_repo.return_value
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr

    mock_file = MagicMock()
    mock_file.filename = "src/webhook_agent/core/test_module.py"
    mock_file.patch = "@@ -10,1 +10,2 @@\n-old\n+new"
    mock_pr.get_files.return_value = [mock_file]

    ctx.state = {
        "gh_client": mock_gh,
        "repo_full_name": "owner/repo",
        "skip_investigation_gate": True,
        "deterministic_changed_files": ["src/webhook_agent/core/test_module.py"],
    }

    report = CodeReviewResponse(
        executive_summary="Audit with candidate findings.",
        verdict="REQUEST_CHANGES",
        scorecard=[
            {"dimension": "Logic & Boundaries", "status": "FAIL", "notes": "Fails"},
            {"dimension": "Concurrency & State", "status": "PASS", "notes": "ok"},
            {"dimension": "Security & Secrets", "status": "PASS", "notes": "ok"},
            {"dimension": "Contract Integrity", "status": "PASS", "notes": "ok"},
            {"dimension": "Test Coverage & Regressions", "status": "PASS", "notes": "ok"},
        ],
        candidate_findings=[
            {
                "id": "cand-crit",
                "file_path": "src/webhook_agent/core/test_module.py",
                "line_number": 10,
                "severity": "CRITICAL",
                "confidence_score": 9,
                "title": "Uncaught crash",
                "description": "Will crash with KeyError",
                "suggested_fix": "return None",
                "falsification_check": "Check KeyError handling",
            },
            {
                "id": "cand-nit",
                "file_path": "src/webhook_agent/core/test_module.py",
                "line_number": 11,
                "severity": "NIT",
                "confidence_score": 9,
                "title": "Comment nit",
                "description": "Fix punctuation",
            },
        ],
    )

    res = review(ctx, pr_number=1, report=report)
    assert "Submitted review (REQUEST_CHANGES)" in res
    assert mock_pr.create_review.called


def test_critic_evaluate_candidate_drops_expensive_verification() -> None:
    critic = CriticEngine(min_confidence=8)
    cand = CandidateFinding(
        id="c-expensive",
        file_path="src/service.py",
        line_number=40,
        severity="HIGH",
        confidence_score=9,
        title="Cross-file deadlock",
        description="Deadlock risk",
        falsification_check="Trace callers across codebase in other files to evaluate locking",
    )
    assert critic.evaluate_candidate(cand) is False


def test_critic_evaluate_candidate_verifies_and_snaps_window() -> None:
    critic = CriticEngine(min_confidence=8)
    file_lines = {
        50: "def execute_task(task_id):",
        51: "    validate_task_id(task_id)",
        52: "    return queue.push(task_id)",
    }
    cand = CandidateFinding(
        id="c-window",
        file_path="src/worker.py",
        line_number=48,  # Model thought line 48 (+2 drift)
        severity="HIGH",
        confidence_score=9,
        title="Missing task validation",
        description="Validation check",
        falsification_check="Verify task_id is checked",
    )
    cand.window = """
    48: def execute_task(task_id):
    49:     validate_task_id(task_id)
    """

    assert critic.evaluate_candidate(cand, file_lines=file_lines) is True
    # The line number was snapped by +2 to line 50!
    assert cand.line_number == 50


def test_critic_evaluate_candidate_drops_fabricated_window() -> None:
    critic = CriticEngine(min_confidence=8)
    file_lines = {
        50: "def execute_task(task_id):",
        51: "    return queue.push(task_id)",
    }
    cand = CandidateFinding(
        id="c-fab",
        file_path="src/worker.py",
        line_number=50,
        severity="HIGH",
        confidence_score=9,
        title="Invented code",
        description="Hallucinated code lines",
        falsification_check="Check code",
    )
    cand.window = """
    50: def completely_invented_method_not_in_file():
    51:     return False
    """
    assert critic.evaluate_candidate(cand, file_lines=file_lines) is False
