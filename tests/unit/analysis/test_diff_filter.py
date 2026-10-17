"""Unit tests for diff filtering and reviewable path inspection."""

from __future__ import annotations

import pytest

from webhook_agent.analysis.diff_filter import (
    budget_for,
    filter_review_diff,
    is_reviewable_path,
    reviewable_file_paths,
    skip_reason,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_reviewable_path_for_source_and_tests():
    assert is_reviewable_path("src/webhook_agent/core/agent_definition.py") is True
    assert is_reviewable_path("tests/unit/analysis/test_diff_filter.py") is True
    assert is_reviewable_path("pyproject.toml") is True
    assert is_reviewable_path("README.md") is True


def test_is_reviewable_path_suppresses_lockfiles_and_vendored():
    assert is_reviewable_path("uv.lock") is False
    assert skip_reason("uv.lock") == "lockfile"

    assert is_reviewable_path("package-lock.json") is False
    assert is_reviewable_path("poetry.lock") is False
    assert is_reviewable_path("vendor/bundle/ruby.rb") is False
    assert is_reviewable_path("node_modules/pkg/index.js") is False


def test_is_reviewable_path_suppresses_assets_and_snapshots():
    assert is_reviewable_path("assets/logo.png") is False
    assert skip_reason("assets/logo.png") == "binary asset"

    assert is_reviewable_path("tests/__snapshots__/test.snap") is False
    assert is_reviewable_path("dist/bundle.min.js") is False


def test_is_reviewable_path_bulk_data_churn():
    # Low churn bulk data is allowed
    assert is_reviewable_path("data/config.json", churn=50) is True

    # High churn bulk data is suppressed
    assert is_reviewable_path("data/big_payload.json", churn=600) is False
    assert skip_reason("data/big_payload.json", churn=600) == "bulk data"


def test_budget_for_churn_levels():
    assert budget_for(20) == 2
    assert budget_for(150) == 3
    assert budget_for(500) == 5


def test_filter_review_diff_empty():
    res = filter_review_diff("")
    assert res.is_reviewable is False
    assert res.reviewable_lines == 0
    assert res.budget == 0


def test_reviewable_file_paths():
    paths = ["src/main.py", "package-lock.json", "uv.lock", "tests/test_foo.py"]
    assert reviewable_file_paths(paths) == ["src/main.py", "tests/test_foo.py"]


def test_filter_review_diff_computes_ast_risk():
    sample_diff = (
        "diff --git a/src/logic.py b/src/logic.py\n"
        "--- a/src/logic.py\n"
        "+++ b/src/logic.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+def compute_total(a: int, b: int) -> int:\n"
        "+    return a + b\n"
    )
    res = filter_review_diff(sample_diff)
    assert res.is_reviewable is True
    assert "src/logic.py" in res.kept_files
    assert res.ast_risk_score > 0.0
    assert "BREAKING_SIGNATURE" in res.ast_impact_counts
