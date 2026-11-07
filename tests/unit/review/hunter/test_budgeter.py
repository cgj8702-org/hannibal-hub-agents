"""Unit tests for ReviewBudgeter churn analysis, budgeting, and file pairing."""

from __future__ import annotations

import pytest

from webhook_agent.review.hunter.budgeter import ReviewBudgeter

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_review_budgeter_skip_reason() -> None:
    budgeter = ReviewBudgeter()

    assert budgeter.skip_reason("package-lock.json") == "lockfile"
    assert budgeter.skip_reason("sub/dir/uv.lock") == "lockfile"
    assert budgeter.skip_reason("vendor/bundle/lib.rb") == "vendored"
    assert budgeter.skip_reason("dist/bundle.min.js") == "build output"
    assert budgeter.skip_reason("logo.png") == "binary asset"
    assert budgeter.skip_reason("src/app_pb2.py") == "generated protobuf"
    assert budgeter.skip_reason("testdata/dump.json") == "test fixture"

    # Bulk data only when churn >= 500
    assert budgeter.skip_reason("data/large_records.json", churn=100) is None
    assert budgeter.skip_reason("data/large_records.json", churn=600) == "bulk data"

    # Regular code files are not skipped
    assert budgeter.skip_reason("src/main.py", churn=150) is None
    assert budgeter.skip_reason("tests/test_main.py", churn=50) is None


def test_review_budgeter_budget_for() -> None:
    budgeter = ReviewBudgeter()

    assert budgeter.budget_for(10) == (2, 3)
    assert budgeter.budget_for(50) == (2, 3)
    assert budgeter.budget_for(51) == (3, 5)
    assert budgeter.budget_for(200) == (3, 5)
    assert budgeter.budget_for(500) == (5, 8)
    assert budgeter.budget_for(1200) == (8, 12)
    assert budgeter.budget_for(5000) == (12, 20)
    # Cap never exceeds 20
    assert budgeter.budget_for(1_000_000)[1] == 20


def test_review_budgeter_pair_sources_and_tests() -> None:
    budgeter = ReviewBudgeter()
    files = [
        "src/auth.py",
        "src/user_service.py",
        "tests/test_auth.py",
        "tests/unit/test_user_service.py",
        "tests/e2e/test_system_integration.py",
    ]

    pairings, unpaired = budgeter.pair_sources_and_tests(files)

    assert "tests/test_auth.py" in pairings["src/auth.py"]
    assert "tests/unit/test_user_service.py" in pairings["src/user_service.py"]
    assert "tests/e2e/test_system_integration.py" in unpaired


def test_review_budgeter_plan_review() -> None:
    budgeter = ReviewBudgeter()
    changed_files = [
        {"filename": "src/app.py", "changes": 45},
        {"filename": "tests/test_app.py", "changes": 30},
        {"filename": "uv.lock", "changes": 2000},
        {"filename": "assets/badge.png", "changes": 10},
    ]

    plan = budgeter.plan_review(changed_files)

    assert plan["total_files"] == 4
    assert plan["reviewable_files"] == ["src/app.py", "tests/test_app.py"]
    assert "uv.lock" in plan["skipped_files"]
    assert "assets/badge.png" in plan["skipped_files"]
    assert plan["reviewable_churn"] == 75
    # Churn of 75 falls in (3, 5) budget tier
    assert plan["budget"]["low"] == 3
    assert plan["budget"]["high"] == 5
    assert plan["budget"]["mid"] == 4
    assert "tests/test_app.py" in plan["source_test_pairings"]["src/app.py"]
