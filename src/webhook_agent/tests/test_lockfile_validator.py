"""Unit tests for deterministic lockfile validator and fast-path approval."""

from webhook_agent.logic.lockfile_validator import (
    is_pure_dependency_pr,
    parse_bumped_packages_from_diff,
    render_deterministic_approval_markdown,
    validate_lockfile_diff,
)

SAMPLE_UV_LOCK_DIFF = """diff --git a/uv.lock b/uv.lock
index 1234..5678 100644
--- a/uv.lock
+++ b/uv.lock
@@ -10,3 +10,3 @@
 [[package]]
 name = "pypdf"
-version = "6.16.1"
+version = "6.19.0"
 source = { registry = "https://pypi.org/simple" }
"""

SAMPLE_MULTI_BUMP_DIFF = """diff --git a/uv.lock b/uv.lock
index 1234..5678 100644
--- a/uv.lock
+++ b/uv.lock
@@ -10,3 +10,3 @@
 [[package]]
 name = "google-cloud-logging"
-version = "3.16.1"
+version = "3.16.3"
 source = { registry = "https://pypi.org/simple" }
@@ -40,3 +40,3 @@
 [[package]]
 name = "urllib3"
-version = "2.7.0"
+version = "2.8.0"
 source = { registry = "https://pypi.org/simple" }
"""

SAMPLE_CONFLICT_DIFF = """diff --git a/uv.lock b/uv.lock
index 1234..5678 100644
--- a/uv.lock
+++ b/uv.lock
<<<<<<< HEAD
 name = "foo"
-version = "1.0.0"
+version = "1.1.0"
=======
 name = "foo"
-version = "1.0.0"
+version = "1.2.0"
>>>>>>> main
"""


def test_is_pure_dependency_pr_dependabot_allowed():
    assert is_pure_dependency_pr(
        sender_login="dependabot[bot]",
        branch_name="dependabot/uv/pypdf-6.19.0",
        changed_files=["uv.lock"],
    )


def test_is_pure_dependency_pr_pyproject_and_lock_allowed():
    assert is_pure_dependency_pr(
        sender_login="dependabot[bot]",
        branch_name="dependabot/uv/pypdf-6.19.0",
        changed_files=["uv.lock", "pyproject.toml"],
    )


def test_is_pure_dependency_pr_mixed_files_rejected():
    # If PR author is dependabot but a code file is touched, reject fast-path
    assert not is_pure_dependency_pr(
        sender_login="dependabot[bot]",
        branch_name="dependabot/uv/pypdf-6.19.0",
        changed_files=["uv.lock", "src/webhook_agent/processor.py"],
    )


def test_is_pure_dependency_pr_human_author_without_dependabot_branch_rejected():
    assert not is_pure_dependency_pr(
        sender_login="human_dev",
        branch_name="feature/my-branch",
        changed_files=["uv.lock"],
    )


def test_parse_bumped_packages_from_diff():
    bumps = parse_bumped_packages_from_diff(SAMPLE_UV_LOCK_DIFF)
    assert len(bumps) == 1
    assert bumps[0]["name"] == "pypdf"
    assert bumps[0]["old_version"] == "6.16.1"
    assert bumps[0]["new_version"] == "6.19.0"

    multi_bumps = parse_bumped_packages_from_diff(SAMPLE_MULTI_BUMP_DIFF)
    assert len(multi_bumps) == 2
    assert multi_bumps[0]["name"] == "google-cloud-logging"
    assert multi_bumps[0]["new_version"] == "3.16.3"
    assert multi_bumps[1]["name"] == "urllib3"
    assert multi_bumps[1]["new_version"] == "2.8.0"


def test_validate_lockfile_diff_clean():
    res = validate_lockfile_diff(SAMPLE_UV_LOCK_DIFF, ["uv.lock"])
    assert res.is_eligible is True
    assert res.should_approve is True
    assert res.bumped_packages == ["pypdf"]
    assert "pypdf" in res.summary


def test_validate_lockfile_diff_conflict_rejected():
    res = validate_lockfile_diff(SAMPLE_CONFLICT_DIFF, ["uv.lock"])
    assert res.is_eligible is True
    assert res.should_approve is False
    assert "conflict markers" in res.summary.lower()


def test_render_deterministic_approval_markdown():
    res = validate_lockfile_diff(SAMPLE_UV_LOCK_DIFF, ["uv.lock"])
    rendered = render_deterministic_approval_markdown(res, 195)
    assert "## 🛡️ Code Review: `APPROVE`" in rendered
    assert "PR #195 is a clean automated dependency update" in rendered
    assert "`pypdf`" in rendered
    assert "#### 🔴 Critical (Must Fix Before Merge)\n* *None found.*" in rendered
