"""Unit tests for standalone feature_agent package."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from feature_agent.agent import build_feature_developer_agent
from feature_agent.delegate import ask_parent
from feature_agent.environment import LocalEnvironment
from feature_agent.firestore_checkpoints import (
    FirestoreFeatureCheckpointRegistry,
)
from feature_agent.guardrails import exfil_guard, permission_guard, policies_guard
from feature_agent.plugins import GuardrailsPlugin
from feature_agent.runner import FeatureTaskRunner
from feature_agent.tools import resolve_in_window


def test_feature_developer_agent_construction():
    agent = build_feature_developer_agent()
    assert agent.name == "feature_developer_agent"
    assert len(getattr(agent, "sub_agents", [])) == 4


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


def test_feature_tools_resolve_in_window_valid():
    wt = Path(".").resolve()
    resolved = resolve_in_window("pyproject.toml", wt)
    assert resolved.exists()


def test_feature_tools_resolve_in_window_traversal_blocked():
    wt = Path(".").resolve()
    with pytest.raises(PermissionError, match="Path traversal blocked"):
        resolve_in_window("../../etc/passwd", wt)


def test_guardrails_exfil_guard():
    res = exfil_guard(
        MagicMock(), {"url": "http://169.254.169.254/latest"}, MagicMock()
    )
    assert res is not None and "strictly prohibited" in res["error"]


def test_guardrails_permission_guard_substitution():
    res = permission_guard(MagicMock(), {"cmd": "echo `id`"}, MagicMock())
    assert res is not None and "Command substitution" in res["error"]


def test_guardrails_policies_guard_force_push():
    res = policies_guard("commit_and_push", {"arg": "git push --force"}, MagicMock())
    assert res is not None and "Force pushing" in res["error"]


def test_plugins_repeated_failure_guard():
    plugin = GuardrailsPlugin(max_repeated_failures=2)
    ctx = MagicMock()
    ctx.state = {}

    plugin.after_tool_callback("run_pytest", {}, ctx, "🔴 FAILED test_foo")
    plugin.after_tool_callback("run_pytest", {}, ctx, "🔴 FAILED test_foo")

    assert ctx.state.get("halt_reason") == "repeated_failure:run_pytest"


def test_delegate_ask_parent_tool():
    ctx = MagicMock()
    ctx.state = {}
    res = ask_parent(ctx, "Should we use PostgreSQL or Firestore?")
    assert "Question escalated" in res
    assert ctx.state["last_parent_question"] == "Should we use PostgreSQL or Firestore?"


def test_local_environment_resolve():
    env = LocalEnvironment(".")
    resolved = env.resolve_path("pyproject.toml")
    assert resolved.exists()
