"""Unit tests for diff filtering and reviewable path inspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.analysis.diff_filter import (
    budget_for,
    extract_enclosing_ast_blocks,
    extract_hunk_ranges,
    filter_review_diff,
    format_enclosing_scopes_markdown,
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
    assert isinstance(res.ast_risk_score, float)
    assert isinstance(res.ast_impact_counts, dict)


def test_filter_review_diff_tier_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify free tier caps diff at 125KB while paid tier allows up to 5MB."""
    # Construct large multi-file diff
    large_sections = []
    for i in range(20):
        large_sections.append(
            f"diff --git a/src/module_{i}.py b/src/module_{i}.py\n"
            f"--- a/src/module_{i}.py\n"
            f"+++ b/src/module_{i}.py\n"
            f"@@ -1,5 +1,100 @@\n" + ("+x = 1\n" * 1500)  # ~10KB per section -> ~200KB total
        )
    huge_diff = "\n".join(large_sections)

    # Free tier -> truncated at 125,000 bytes
    monkeypatch.setenv("WEBHOOK_TIER", "free")
    res_free = filter_review_diff(huge_diff)
    assert "[... remaining files truncated to fit 125000 byte budget ...]" in res_free.filtered_diff
    assert len(res_free.filtered_diff.encode("utf-8")) <= 135000

    # Paid tier -> 5MB budget, no truncation
    monkeypatch.setenv("WEBHOOK_TIER", "paid")
    res_paid = filter_review_diff(huge_diff)
    assert "remaining files truncated" not in res_paid.filtered_diff
    assert len(res_paid.filtered_diff.encode("utf-8")) > 150000


def test_extract_hunk_ranges() -> None:
    diff_text = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,2 +15,5 @@ def foo():\n"
        "+    return 42\n"
        "@@ -50,1 +60,3 @@ def bar():\n"
        "+    pass\n"
    )
    ranges = extract_hunk_ranges(diff_text)
    assert "src/app.py" in ranges
    assert ranges["src/app.py"] == [(15, 19), (60, 62)]


def test_extract_enclosing_ast_blocks_and_markdown(tmp_path: Path) -> None:
    # Create sample Python source
    py_file = tmp_path / "src" / "worker.py"
    py_file.parent.mkdir(parents=True, exist_ok=True)
    py_file.write_text(
        "class Worker:\n"
        "    def run(self) -> None:\n"
        "        x = 1\n"
        "        y = 2\n"
        "        # modified line\n"
        "        return x + y\n"
        "\n"
        "def standalone_func(val: int) -> int:\n"
        "    res = val * 2\n"
        "    return res\n"
    )

    # Diff modifying line 5 inside Worker.run
    diff_text = (
        "diff --git a/src/worker.py b/src/worker.py\n"
        "--- a/src/worker.py\n"
        "+++ b/src/worker.py\n"
        "@@ -4,3 +4,4 @@\n"
        "         y = 2\n"
        "+        z = 3\n"
        "         # modified line\n"
    )

    blocks = extract_enclosing_ast_blocks(diff_text, repo_root=tmp_path)
    assert len(blocks) == 1
    assert blocks[0].file_path == "src/worker.py"
    assert blocks[0].scope_name == "def Worker.run"
    assert blocks[0].start_line == 2
    assert "def run(self) -> None:" in blocks[0].code

    md = format_enclosing_scopes_markdown(blocks)
    assert "### 🧩 Enclosing AST Scopes for Modified Hunks" in md
    assert "def Worker.run" in md
    assert "src/worker.py" in md

    # filter_review_diff with repo_root populated
    res = filter_review_diff(diff_text, repo_root=tmp_path)
    assert len(res.enclosing_scopes) == 1
    assert "def Worker.run" in res.enclosing_scopes_markdown
