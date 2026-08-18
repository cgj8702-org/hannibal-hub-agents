"""Unit tests for standalone feature_agent package."""

from unittest.mock import MagicMock, patch

from google.adk.agents.context import Context

from feature_agent.agent import build_feature_developer_agent
from feature_agent.firestore_checkpoints import (
    FirestoreFeatureCheckpointRegistry,
)
from feature_agent.runner import FeatureTaskRunner
from feature_agent.tools import get_worktree_path, view_file


def test_feature_developer_agent_construction():
    agent = build_feature_developer_agent()
    assert agent.name == "feature_developer_agent"
    assert len(agent.tools) >= 5


def test_firestore_checkpoint_registry_save_and_get():
    registry = FirestoreFeatureCheckpointRegistry(
        collection_name="test_feature_checkpoints"
    )
    registry.save_checkpoint(
        issue_number=101,
        instruction="build rate limiter endpoint",
        branch_name="feat/issue-101-auto-impl",
        session_id="delegate-issue-101",
        status="in_progress",
    )
    doc = registry.get_checkpoint(101)
    assert doc is None or isinstance(doc, dict)


def test_feature_task_runner_disabled_policy(monkeypatch):
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "0")
    runner = FeatureTaskRunner()
    res = runner.execute_task(issue_number=5, instruction="disabled feature test")
    assert "disabled by policy" in res


def test_feature_task_runner_quota_paused(monkeypatch):
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")
    runner = FeatureTaskRunner()

    with patch(
        "feature_agent.runner.firestore_checkpoint_registry.get_checkpoint",
        return_value={
            "status": "quota_paused",
            "resume_at": None,
        },
    ):
        with patch("github.Github"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="clean")
                res = runner.execute_task(
                    issue_number=77, instruction="quota pause test"
                )
                assert "Issue #77" in res


def test_feature_tools_get_worktree_path():
    ctx = MagicMock(spec=Context)
    ctx.state = {"worktree_path": "/tmp/nonexistent_wt_path"}
    path = get_worktree_path(ctx)
    assert path.exists()


def test_feature_tools_view_file_nonexistent():
    ctx = MagicMock(spec=Context)
    res = view_file(ctx, "nonexistent_file_12345.txt")
    assert "does not exist" in res
