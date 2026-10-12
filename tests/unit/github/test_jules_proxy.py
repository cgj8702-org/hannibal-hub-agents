"""Unit tests for the Jules PR Adoption Proxy."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.github.jules_proxy import (
    format_superseded_body,
    get_agents_github_client,
    get_agents_github_token,
    handle_jules_pr_opened,
    reopen_jules_pr_as_agent,
)
from webhook_agent.processor import WebhookProcessor

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_get_agents_github_token_env_priority(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "token_from_cgj_var")
    monkeypatch.setenv("AGENTS_GITHUB_TOKEN", "token_from_agents_var")
    assert get_agents_github_token() == "token_from_cgj_var"

    monkeypatch.delenv("CGJ8702_AGENTS_TOKEN")
    assert get_agents_github_token() == "token_from_agents_var"


def test_get_agents_github_token_fallback_gh(monkeypatch):
    monkeypatch.delenv("CGJ8702_AGENTS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with patch("shutil.which", return_value="/usr/bin/gh"):
        mock_proc = MagicMock(returncode=0, stdout="gh_cli_token\n")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            assert get_agents_github_token() == "gh_cli_token"
            mock_run.assert_called_once_with(
                ["/usr/bin/gh", "auth", "token", "--user", "cgj8702-agents"],
                capture_output=True,
                text=True,
                timeout=5,
            )


def test_get_agents_github_token_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("CGJ8702_AGENTS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with patch("shutil.which", return_value=None):
        assert get_agents_github_token() is None


def test_get_agents_github_client(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "ghp_fake_token_12345")
    client = get_agents_github_client()
    assert client is not None

    monkeypatch.delenv("CGJ8702_AGENTS_TOKEN")
    with patch("shutil.which", return_value=None):
        assert get_agents_github_client() is None


def test_format_superseded_body():
    orig = "This is a feature implemented by Jules.\n\nCloses #12."
    formatted = format_superseded_body(orig, 284)
    assert orig in formatted
    assert "Reposted from #284" in formatted
    assert "CodeRabbit skips bot-authored PRs" in formatted
    assert "opened under cgj8702-agents" in formatted
    assert "Closes #284 on merge" in formatted


def test_reopen_jules_pr_as_agent_missing_data():
    gh_app = MagicMock()
    # Missing PR data
    assert reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", {}) is None

    # Closed or merged PR
    payload_closed = {
        "raw_payload": {
            "pull_request": {"number": 10, "state": "closed", "head": {"ref": "jules-fix"}}
        }
    }
    assert reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload_closed) is None

    payload_merged = {
        "raw_payload": {
            "pull_request": {
                "number": 10,
                "state": "open",
                "merged": True,
                "head": {"ref": "jules-fix"},
            }
        }
    }
    assert reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload_merged) is None

    # Missing head ref
    payload_no_head = {"raw_payload": {"pull_request": {"number": 10, "state": "open", "head": {}}}}
    assert reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload_no_head) is None


def test_reopen_jules_pr_as_agent_dry_run():
    gh_app = MagicMock()
    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 284,
                "state": "open",
                "title": "feat: test",
                "head": {"ref": "jules-test-branch"},
                "base": {"ref": "main"},
            }
        }
    }
    res = reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=True)
    assert res == 999999
    gh_app.get_repo.assert_not_called()


def test_reopen_jules_pr_as_agent_no_token(monkeypatch):
    monkeypatch.delenv("CGJ8702_AGENTS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with patch("shutil.which", return_value=None):
        gh_app = MagicMock()
        payload = {
            "raw_payload": {
                "pull_request": {
                    "number": 284,
                    "state": "open",
                    "title": "feat: test",
                    "head": {"ref": "jules-test-branch"},
                    "base": {"ref": "main"},
                }
            }
        }
        res = reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=False)
        assert res is None
        gh_app.get_repo.assert_not_called()


def test_reopen_jules_pr_as_agent_happy_path(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "mock_token")

    gh_app = MagicMock()
    app_repo = MagicMock()
    original_pr = MagicMock(state="open", merged=False)
    gh_app.get_repo.return_value = app_repo
    app_repo.get_pull.return_value = original_pr

    mock_agents_gh = MagicMock()
    agent_repo = MagicMock()
    new_pr = MagicMock(number=290)
    mock_agents_gh.get_repo.return_value = agent_repo
    agent_repo.create_pull.return_value = new_pr

    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 284,
                "state": "open",
                "title": "feat: awesome feature",
                "body": "Original PR description",
                "draft": False,
                "head": {
                    "ref": "jules-fix-feature",
                    "repo": {"owner": {"login": "cgj8702-org"}},
                },
                "base": {
                    "ref": "main",
                    "repo": {"owner": {"login": "cgj8702-org"}},
                },
                "labels": [{"name": "enhancement"}, {"name": "jules"}],
            }
        }
    }

    with patch(
        "webhook_agent.github.jules_proxy.get_agents_github_client",
        return_value=mock_agents_gh,
    ):
        new_num = reopen_jules_pr_as_agent(
            gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=False
        )

    assert new_num == 290

    # 1. Verify original PR closed
    original_pr.create_issue_comment.assert_any_call(
        "Closing in favor of reopening an identical PR authored by @cgj8702-agents for CodeRabbit AI review compatibility."
    )
    original_pr.edit.assert_called_once_with(state="closed")

    # 2. Verify new PR created on exact same branch
    agent_repo.create_pull.assert_called_once()
    create_kwargs = agent_repo.create_pull.call_args.kwargs
    assert create_kwargs["title"] == "feat: awesome feature"
    assert create_kwargs["head"] == "jules-fix-feature"
    assert create_kwargs["base"] == "main"
    assert "Original PR description" in create_kwargs["body"]
    assert "Reposted from #284" in create_kwargs["body"]

    # 3. Verify labels transferred
    new_pr.add_to_labels.assert_called_once_with("enhancement", "jules")

    # 4. Verify original PR marked in registry
    assert pr_closed_registry.is_closed("cgj8702-org/hannibal-hub", 284) is True

    # 5. Verify superseded link comment
    original_pr.create_issue_comment.assert_any_call(
        "Superseded by #290 (authored by @cgj8702-agents)."
    )


def test_reopen_jules_pr_as_agent_fork_handling(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "mock_token")

    gh_app = MagicMock()
    app_repo = MagicMock()
    original_pr = MagicMock(state="open", merged=False)
    gh_app.get_repo.return_value = app_repo
    app_repo.get_pull.return_value = original_pr

    mock_agents_gh = MagicMock()
    agent_repo = MagicMock()
    new_pr = MagicMock(number=300)
    mock_agents_gh.get_repo.return_value = agent_repo
    agent_repo.create_pull.return_value = new_pr

    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 285,
                "state": "open",
                "title": "feat: fork feature",
                "head": {
                    "ref": "jules-fork-branch",
                    "repo": {"owner": {"login": "jules-external"}},
                },
                "base": {
                    "ref": "main",
                    "repo": {"owner": {"login": "cgj8702-org"}},
                },
            }
        }
    }

    with patch(
        "webhook_agent.github.jules_proxy.get_agents_github_client",
        return_value=mock_agents_gh,
    ):
        new_num = reopen_jules_pr_as_agent(
            gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=False
        )

    assert new_num == 300
    create_kwargs = agent_repo.create_pull.call_args.kwargs
    assert create_kwargs["head"] == "jules-external:jules-fork-branch"


def test_reopen_jules_pr_as_agent_rollback_on_failure(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "mock_token")

    gh_app = MagicMock()
    app_repo = MagicMock()
    original_pr = MagicMock(state="open", merged=False)
    gh_app.get_repo.return_value = app_repo
    app_repo.get_pull.return_value = original_pr

    mock_agents_gh = MagicMock()
    agent_repo = MagicMock()
    agent_repo.create_pull.side_effect = RuntimeError("GitHub 422: branch error")
    mock_agents_gh.get_repo.return_value = agent_repo

    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 286,
                "state": "open",
                "title": "feat: will fail",
                "head": {"ref": "jules-fail-branch"},
                "base": {"ref": "main"},
            }
        }
    }

    with patch(
        "webhook_agent.github.jules_proxy.get_agents_github_client",
        return_value=mock_agents_gh,
    ):
        result = reopen_jules_pr_as_agent(
            gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=False
        )

    assert result is None
    # Verify rollback: edit called with state="open" after initial closed edit
    assert original_pr.edit.call_count == 2
    assert original_pr.edit.call_args_list[0].kwargs == {"state": "closed"}
    assert original_pr.edit.call_args_list[1].kwargs == {"state": "open"}


def test_reopen_jules_pr_as_agent_skips_when_live_pr_closed(monkeypatch):
    monkeypatch.setenv("CGJ8702_AGENTS_TOKEN", "mock_token")

    gh_app = MagicMock()
    app_repo = MagicMock()
    original_pr = MagicMock(state="closed", merged=False)
    gh_app.get_repo.return_value = app_repo
    app_repo.get_pull.return_value = original_pr

    mock_agents_gh = MagicMock()

    payload = {
        "raw_payload": {
            "pull_request": {
                "number": 284,
                "state": "open",
                "head": {"ref": "jules-branch"},
                "base": {"ref": "main"},
            }
        }
    }

    with patch(
        "webhook_agent.github.jules_proxy.get_agents_github_client",
        return_value=mock_agents_gh,
    ):
        res = reopen_jules_pr_as_agent(gh_app, "cgj8702-org/hannibal-hub", payload, dry_run=False)

    assert res is None
    original_pr.edit.assert_not_called()
    mock_agents_gh.get_repo.assert_not_called()


def test_handle_jules_pr_opened():
    gh_app = MagicMock()

    # Case 1: Non-opened action
    res = handle_jules_pr_opened(
        gh_app,
        "cgj8702-org/hannibal-hub",
        {
            "raw_payload": {
                "action": "synchronize",
                "pull_request": {
                    "number": 10,
                    "user": {"login": "google-labs-jules[bot]"},
                },
            }
        },
    )
    assert res is False

    # Case 2: Opened action, but non-Jules sender
    res = handle_jules_pr_opened(
        gh_app,
        "cgj8702-org/hannibal-hub",
        {
            "canonical": "pull_request.opened",
            "raw_payload": {
                "action": "opened",
                "pull_request": {
                    "number": 10,
                    "user": {"login": "human-developer"},
                },
            },
        },
    )
    assert res is False

    # Case 3: Opened action by Jules, replacement succeeds
    with patch(
        "webhook_agent.github.jules_proxy.reopen_jules_pr_as_agent",
        return_value=291,
    ) as mock_reopen:
        res = handle_jules_pr_opened(
            gh_app,
            "cgj8702-org/hannibal-hub",
            {
                "canonical": "pull_request.opened",
                "raw_payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": 284,
                        "user": {"login": "google-labs-jules[bot]"},
                    },
                },
            },
        )
        assert res is True
        mock_reopen.assert_called_once()


def test_processor_short_circuits_on_jules_pr_opened():
    processor = WebhookProcessor()
    processor.dry_run = True
    processor._gh = MagicMock()
    payload = {
        "event_name": "pull_request",
        "action": "opened",
        "repository": {"full_name": "cgj8702-org/hannibal-hub"},
        "raw_payload": {
            "action": "opened",
            "repository": {"full_name": "cgj8702-org/hannibal-hub"},
            "pull_request": {
                "number": 284,
                "state": "open",
                "user": {"login": "google-labs-jules[bot]"},
                "head": {"ref": "jules-branch", "sha": "abcdef12345"},
                "base": {"ref": "main"},
            },
        },
    }

    with patch(
        "webhook_agent.processor.handle_jules_pr_opened",
        return_value=True,
    ) as mock_handle:
        with patch.object(processor, "_get_agent_core") as mock_agent_core:
            processor.process_event(payload)
            mock_handle.assert_called_once()
            # Since handle_jules_pr_opened returned True, agent_core must not be called
            mock_agent_core.return_value.run.assert_not_called()
