"""Unit tests for tool input Pydantic schemas and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from webhook_agent.tools.schemas import (
    TOOL_INPUT_SCHEMAS,
    AddLabelInput,
    CreateIssueInput,
    GetCommitDiffInput,
    GetIssueInput,
    ReadFileInput,
    ReviewInput,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_read_file_input():
    # Valid
    inp = ReadFileInput.model_validate({"file_path": "src/main.py", "ref": "feat-branch"})
    assert inp.file_path == "src/main.py"
    assert inp.ref == "feat-branch"

    # Missing file_path
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({})

    # Empty file_path
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({"file_path": ""})


def test_get_issue_input():
    # Valid with number
    inp = GetIssueInput.model_validate({"number": 42, "include_diff": True})
    assert inp.number == 42
    assert inp.include_diff is True

    # Coerced from issue_number
    inp2 = GetIssueInput.model_validate({"issue_number": "99"})
    assert inp2.number == 99

    # Coerced from pr_number
    inp3 = GetIssueInput.model_validate({"pr_number": 101})
    assert inp3.number == 101

    # Invalid number
    with pytest.raises(ValidationError):
        GetIssueInput.model_validate({"number": 0})


def test_get_commit_diff_input():
    # Valid
    inp = GetCommitDiffInput.model_validate({"base_sha": "abc1234", "head_sha": "def5678"})
    assert inp.base_sha == "abc1234"
    assert inp.head_sha == "def5678"

    # Missing head_sha
    with pytest.raises(ValidationError):
        GetCommitDiffInput.model_validate({"base_sha": "abc"})


def test_review_input():
    # Valid
    inp = ReviewInput.model_validate(
        {"pr_number": 55, "body": '{"verdict": "APPROVE"}', "event": "APPROVE"}
    )
    assert inp.pr_number == 55
    assert inp.event == "APPROVE"

    # Coerced event case and issue_number alias
    inp2 = ReviewInput.model_validate(
        {"issue_number": "60", "body": "looks good", "event": "comment"}
    )
    assert inp2.pr_number == 60
    assert inp2.event == "COMMENT"

    # Invalid event
    with pytest.raises(ValidationError):
        ReviewInput.model_validate({"pr_number": 1, "body": "test", "event": "INVALID"})


def test_add_label_input():
    # Valid list
    inp = AddLabelInput.model_validate({"issue_number": 12, "labels": ["jules", "bug"]})
    assert inp.issue_number == 12
    assert inp.labels == ["jules", "bug"]

    # Coerced comma-separated string to list
    inp2 = AddLabelInput.model_validate({"pr_number": "15", "labels": "jules, enhancement"})
    assert inp2.issue_number == 15
    assert inp2.labels == ["jules", "enhancement"]

    # Empty labels
    with pytest.raises(ValidationError):
        AddLabelInput.model_validate({"issue_number": 1, "labels": []})


def test_create_issue_input():
    # Valid
    inp = CreateIssueInput.model_validate(
        {
            "title": "Bug in worker",
            "body": "Fix the worker crash",
            "labels": ["jules"],
        }
    )
    assert inp.title == "Bug in worker"
    assert inp.body == "Fix the worker crash"
    assert inp.labels == ["jules"]

    # Missing title or body
    with pytest.raises(ValidationError):
        CreateIssueInput.model_validate({"title": "Test"})
    with pytest.raises(ValidationError):
        CreateIssueInput.model_validate({"body": "Test"})


def test_tool_input_schemas_registry():
    assert "read_file" in TOOL_INPUT_SCHEMAS
    assert "get_issue" in TOOL_INPUT_SCHEMAS
    assert "get_commit_diff" in TOOL_INPUT_SCHEMAS
    assert "review" in TOOL_INPUT_SCHEMAS
    assert "add_label" in TOOL_INPUT_SCHEMAS
    assert "create_issue" in TOOL_INPUT_SCHEMAS
    # Regression guard: unguarded mutating tools must not be re-registered without
    # authorization gates. See the reviewer hardening plan, Phase 6.
    assert "update_issue" not in TOOL_INPUT_SCHEMAS
    assert "merge_pr" not in TOOL_INPUT_SCHEMAS
