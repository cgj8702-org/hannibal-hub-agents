"""Tests for agent_core.py — tool validators, writeback policy, rule-based planning."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from webhook_agent.agent_core import (
    AgentCore,
    CanonicalEvent,
    ToolValidationError,
    check_writeback_policy,
    generate_trace_id,
    validate_create_issue_args,
    validate_add_comment_args,
    validate_create_branch_commit_args,
    validate_open_pr_args,
    validate_add_review_comment_args,
    validate_merge_pr_args,
    validate_add_label_args,
    validate_assign_reviewers_args,
    validate_reply_to_review_comment_args,
    validate_submit_review_args,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_canonical_event(
    canonical: str = "pull_request.opened",
    sender_login: str = "test-user",
    delivery_id: str = "del-001",
) -> CanonicalEvent:
    return CanonicalEvent(
        canonical=canonical,
        delivery_id=delivery_id,
        event_name=canonical.split(".")[0],
        action=canonical.split(".")[1] if "." in canonical else None,
        sender={"login": sender_login, "type": "User"},
        installation={"id": 12345},
        repository={"full_name": "owner/repo", "owner": {"login": "owner"}},
        raw_payload={
            "action": canonical.split(".")[1] if "." in canonical else None,
            "pull_request": {"number": 42, "title": "Test PR"},
        },
    )


# ---------------------------------------------------------------------------
# Tests: generate_trace_id
# ---------------------------------------------------------------------------


class TestGenerateTraceId:
    def test_returns_hex_string(self):
        tid = generate_trace_id()
        assert isinstance(tid, str)
        assert len(tid) == 32  # 16 bytes = 32 hex chars
        assert all(c in "0123456789abcdef" for c in tid)

    def test_unique_ids(self):
        ids = {generate_trace_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Tests: Tool validators
# ---------------------------------------------------------------------------


class TestValidateCreateIssueArgs:
    def test_valid(self):
        validate_create_issue_args({"title": "Fix bug", "body": "Details"})

    def test_missing_title(self):
        with pytest.raises(ToolValidationError, match="title"):
            validate_create_issue_args({"body": "Details"})

    def test_empty_title(self):
        with pytest.raises(ToolValidationError, match="title"):
            validate_create_issue_args({"title": ""})

    def test_non_string_body(self):
        with pytest.raises(ToolValidationError, match="body"):
            validate_create_issue_args({"title": "Fix", "body": 123})


class TestValidateAddCommentArgs:
    def test_valid(self):
        validate_add_comment_args({"issue_number": 1, "body": "Nice work"})

    def test_missing_issue_number(self):
        with pytest.raises(ToolValidationError, match="issue_number"):
            validate_add_comment_args({"body": "Nice"})

    def test_non_int_issue_number(self):
        with pytest.raises(ToolValidationError, match="issue_number"):
            validate_add_comment_args({"issue_number": "1", "body": "Nice"})

    def test_empty_body(self):
        with pytest.raises(ToolValidationError, match="body"):
            validate_add_comment_args({"issue_number": 1, "body": ""})


class TestValidateCreateBranchCommitArgs:
    def test_valid(self):
        validate_create_branch_commit_args(
            {
                "branch_name": "feature-x",
                "file_path": "src/main.py",
                "file_content": "print('hello')",
            }
        )

    def test_missing_branch_name(self):
        with pytest.raises(ToolValidationError, match="branch_name"):
            validate_create_branch_commit_args(
                {
                    "file_path": "src/main.py",
                    "file_content": "x",
                }
            )

    def test_missing_file_path(self):
        with pytest.raises(ToolValidationError, match="file_path"):
            validate_create_branch_commit_args(
                {
                    "branch_name": "feat",
                    "file_content": "x",
                }
            )

    def test_missing_file_content(self):
        with pytest.raises(ToolValidationError, match="file_content"):
            validate_create_branch_commit_args(
                {
                    "branch_name": "feat",
                    "file_path": "src/main.py",
                }
            )


class TestValidateOpenPrArgs:
    def test_valid(self):
        validate_open_pr_args(
            {
                "head_branch": "feature",
                "base_branch": "main",
                "title": "New feature",
            }
        )

    def test_missing_head(self):
        with pytest.raises(ToolValidationError, match="head_branch"):
            validate_open_pr_args({"base_branch": "main", "title": "X"})

    def test_missing_base(self):
        with pytest.raises(ToolValidationError, match="base_branch"):
            validate_open_pr_args({"head_branch": "feat", "title": "X"})

    def test_missing_title(self):
        with pytest.raises(ToolValidationError, match="title"):
            validate_open_pr_args({"head_branch": "feat", "base_branch": "main"})


class TestValidateAddReviewCommentArgs:
    def test_valid(self):
        validate_add_review_comment_args({"pr_number": 1, "body": "Looks good"})

    def test_missing_pr_number(self):
        with pytest.raises(ToolValidationError, match="pr_number"):
            validate_add_review_comment_args({"body": "Nice"})

    def test_empty_body(self):
        with pytest.raises(ToolValidationError, match="body"):
            validate_add_review_comment_args({"pr_number": 1, "body": ""})


class TestValidateMergePrArgs:
    def test_valid_default_method(self):
        validate_merge_pr_args({"pr_number": 1})

    def test_valid_with_method(self):
        validate_merge_pr_args({"pr_number": 1, "merge_method": "squash"})

    def test_invalid_method(self):
        with pytest.raises(ToolValidationError, match="merge_method"):
            validate_merge_pr_args({"pr_number": 1, "merge_method": "invalid"})

    def test_missing_pr_number(self):
        with pytest.raises(ToolValidationError, match="pr_number"):
            validate_merge_pr_args({})


class TestValidateAddLabelArgs:
    def test_valid(self):
        validate_add_label_args({"labels": ["bug", "urgent"]})

    def test_non_list_labels(self):
        with pytest.raises(ToolValidationError, match="labels"):
            validate_add_label_args({"labels": "bug"})

    def test_non_string_in_labels(self):
        with pytest.raises(ToolValidationError, match="labels"):
            validate_add_label_args({"labels": ["bug", 123]})


class TestValidateAssignReviewersArgs:
    def test_valid(self):
        validate_assign_reviewers_args({"pr_number": 1, "reviewers": ["user1"]})

    def test_missing_pr_number(self):
        with pytest.raises(ToolValidationError, match="pr_number"):
            validate_assign_reviewers_args({"reviewers": ["user1"]})

    def test_non_list_reviewers(self):
        with pytest.raises(ToolValidationError, match="reviewers"):
            validate_assign_reviewers_args({"pr_number": 1, "reviewers": "user1"})


class TestValidateReplyToReviewCommentArgs:
    def test_valid(self):
        validate_reply_to_review_comment_args(
            {
                "pr_number": 1,
                "comment_id": 42,
                "body": "Thanks!",
            }
        )

    def test_missing_comment_id(self):
        with pytest.raises(ToolValidationError, match="comment_id"):
            validate_reply_to_review_comment_args(
                {
                    "pr_number": 1,
                    "body": "Thanks",
                }
            )

    def test_empty_body(self):
        with pytest.raises(ToolValidationError, match="body"):
            validate_reply_to_review_comment_args(
                {
                    "pr_number": 1,
                    "comment_id": 42,
                    "body": "",
                }
            )


class TestValidateSubmitReviewArgs:
    def test_valid_default_event(self):
        validate_submit_review_args({"pr_number": 1, "body": "LGTM"})

    def test_valid_approve(self):
        validate_submit_review_args(
            {
                "pr_number": 1,
                "body": "LGTM",
                "event": "APPROVE",
            }
        )

    def test_invalid_event(self):
        with pytest.raises(ToolValidationError, match="event"):
            validate_submit_review_args(
                {
                    "pr_number": 1,
                    "body": "LGTM",
                    "event": "INVALID",
                }
            )

    def test_missing_body(self):
        with pytest.raises(ToolValidationError, match="body"):
            validate_submit_review_args({"pr_number": 1})


# ---------------------------------------------------------------------------
# Tests: check_writeback_policy
# ---------------------------------------------------------------------------


class TestCheckWritebackPolicy:
    def test_human_sender_allowed(self):
        event = _make_canonical_event("pull_request.opened", sender_login="human")
        action = {"tool": "add_comment", "args": {"issue_number": 1, "body": "Hi"}}
        assert check_writeback_policy(event, action) is None

    def test_bot_sender_blocked(self):
        event = _make_canonical_event(
            "pull_request.opened", sender_login="hannibal-hub-agents[bot]"
        )
        action = {"tool": "add_comment", "args": {"issue_number": 1, "body": "Hi"}}
        reason = check_writeback_policy(event, action)
        assert reason is not None
        assert "bot-authored" in reason

    def test_read_only_event_blocked(self):
        for ro_event in [
            "pull_request.synchronize",
            "pull_request.closed",
            "label.deleted",
            "installation.created",
            "ping",
            "unknown",
        ]:
            event = _make_canonical_event(ro_event, sender_login="human")
            action = {"tool": "add_comment", "args": {"issue_number": 1, "body": "Hi"}}
            reason = check_writeback_policy(event, action)
            assert reason is not None, f"expected {ro_event} to be blocked"
            assert "read-only" in reason

    def test_mutable_event_allowed(self):
        for ok_event in [
            "pull_request.opened",
            "issue_comment.created",
            "pull_request_review_comment.created",
            "pull_request_review.submitted",
            "pull_request_review_requested",
            "label.created",
        ]:
            event = _make_canonical_event(ok_event, sender_login="human")
            action = {"tool": "add_comment", "args": {"issue_number": 1, "body": "Hi"}}
            assert check_writeback_policy(event, action) is None, (
                f"expected {ok_event} to be allowed"
            )


# ---------------------------------------------------------------------------
# Tests: AgentCore._rule_based_plan
# ---------------------------------------------------------------------------


class TestRuleBasedPlan:
    def test_pull_request_opened_adds_comment(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = _make_canonical_event("pull_request.opened")
        actions = core._rule_based_plan(event, "trace-001")
        assert len(actions) == 1
        assert actions[0]["tool"] == "add_comment"
        assert actions[0]["args"]["issue_number"] == 42

    def test_pull_request_synchronize_no_action(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = _make_canonical_event("pull_request.synchronize")
        actions = core._rule_based_plan(event, "trace-002")
        assert len(actions) == 0

    def test_issue_comment_created_with_keyword(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = CanonicalEvent(
            canonical="issue_comment.created",
            delivery_id="del-003",
            event_name="issue_comment",
            action="created",
            sender={"login": "user", "type": "User"},
            installation={"id": 1},
            repository={"full_name": "owner/repo"},
            raw_payload={
                "issue": {"number": 10},
                "comment": {"body": "/review please"},
                "action": "created",
            },
        )
        actions = core._rule_based_plan(event, "trace-003")
        assert len(actions) == 1
        assert actions[0]["tool"] == "add_comment"
        assert actions[0]["args"]["issue_number"] == 10

    def test_issue_comment_created_without_keyword(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = CanonicalEvent(
            canonical="issue_comment.created",
            delivery_id="del-004",
            event_name="issue_comment",
            action="created",
            sender={"login": "user", "type": "User"},
            installation={"id": 1},
            repository={"full_name": "owner/repo"},
            raw_payload={
                "issue": {"number": 10},
                "comment": {"body": "Just a normal comment"},
                "action": "created",
            },
        )
        actions = core._rule_based_plan(event, "trace-004")
        assert len(actions) == 0

    def test_pull_request_review_requested_adds_comment(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = _make_canonical_event("pull_request_review_requested")
        actions = core._rule_based_plan(event, "trace-005")
        assert len(actions) == 1
        assert actions[0]["tool"] == "add_comment"

    def test_ping_no_action(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = _make_canonical_event("ping")
        actions = core._rule_based_plan(event, "trace-006")
        assert len(actions) == 0

    def test_unknown_event_no_action(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        event = _make_canonical_event("unknown")
        actions = core._rule_based_plan(event, "trace-007")
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# Tests: AgentCore.validate_action
# ---------------------------------------------------------------------------


class TestAgentCoreValidateAction:
    def test_valid_tool(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        core.validate_action(
            {
                "tool": "add_comment",
                "args": {"issue_number": 1, "body": "Hi"},
            }
        )

    def test_unknown_tool(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        with pytest.raises(ToolValidationError, match="unknown tool"):
            core.validate_action({"tool": "nonexistent_tool", "args": {}})

    def test_invalid_args(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        with pytest.raises(ToolValidationError, match="title"):
            core.validate_action(
                {
                    "tool": "create_issue",
                    "args": {"body": "missing title"},
                }
            )


# ---------------------------------------------------------------------------
# Tests: AgentCore.run (integration with writeback policy)
# ---------------------------------------------------------------------------


class TestAgentCoreRun:
    def test_dry_run_returns_success(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-001",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is True
        assert "dry-run" in results[0].detail

    def test_bot_sender_blocked_by_writeback_policy(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-002",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "hannibal-hub-agents[bot]"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is False
        assert "writeback policy" in results[0].detail

    def test_read_only_event_blocked(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-003",
            "event_name": "pull_request",
            "action": "synchronize",
            "canonical": "pull_request.synchronize",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "synchronize"},
        }
        results = core.run(ev, "owner/repo")
        # synchronize is read-only, so rule-based plan returns no actions
        assert len(results) == 0

    def test_infer_canonical_from_event_data(self):
        core = AgentCore(gh_client=MagicMock(), dry_run=True)
        ev = {
            "delivery_id": "test-004",
            "event_name": "pull_request",
            "action": "opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {"pull_request": {"number": 1}, "action": "opened"},
        }
        # No 'canonical' key — should be inferred
        results = core.run(ev, "owner/repo")
        assert len(results) == 1
        assert results[0].success is True

    def test_update_pr_description_marks_ready_for_review(self):
        # Test non-dry-run mode to verify the ready_for_review logic
        core = AgentCore(gh_client=MagicMock(), dry_run=False)
        ev = {
            "delivery_id": "test-005",
            "event_name": "pull_request",
            "action": "opened",
            "canonical": "pull_request.opened",
            "sender": {"login": "human"},
            "repository": {"full_name": "owner/repo"},
            "raw_payload": {
                "pull_request": {"number": 42, "body": "/create"},
                "action": "opened",
            },
        }
        # Need to mock the environment to allow mutations
        import os

        old_val = os.environ.get("ALLOW_AUTOMATED_MUTATIONS")
        os.environ["ALLOW_AUTOMATED_MUTATIONS"] = "1"
        try:
            results = core.run(ev, "owner/repo")
            assert len(results) == 1
            assert results[0].success is True
            assert "ready for review" in results[0].detail
        finally:
            if old_val is None:
                os.environ.pop("ALLOW_AUTOMATED_MUTATIONS", None)
            else:
                os.environ["ALLOW_AUTOMATED_MUTATIONS"] = old_val
