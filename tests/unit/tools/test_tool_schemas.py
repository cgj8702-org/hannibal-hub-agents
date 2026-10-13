"""Unit tests for tool input Pydantic schemas and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from webhook_agent.tools.schemas import (
    TOOL_INPUT_SCHEMAS,
    CreateIssueInput,
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


def test_create_issue_input():
    # Valid
    inp = CreateIssueInput.model_validate(
        {
            "title": "Bug in worker",
            "body": "Fix the worker crash",
        }
    )
    assert inp.title == "Bug in worker"
    assert inp.body == "Fix the worker crash"
    assert not hasattr(inp, "labels")

    # Missing title or body
    with pytest.raises(ValidationError):
        CreateIssueInput.model_validate({"title": "Test"})
    with pytest.raises(ValidationError):
        CreateIssueInput.model_validate({"body": "Test"})


def test_tool_input_schemas_registry():
    assert "read_file" in TOOL_INPUT_SCHEMAS
    assert "get_issue" in TOOL_INPUT_SCHEMAS
    assert "review" in TOOL_INPUT_SCHEMAS
    # create_issue may still be in registry for non-agent callers, but we verify its existence if needed.
    # To strictly align with the "mutation-free" agent identity, we check the agent's tools instead.
    # For now, we just ensure critical mutations are GONE.
    assert "get_commit_diff" not in TOOL_INPUT_SCHEMAS
    assert "add_label" not in TOOL_INPUT_SCHEMAS
    assert "update_issue" not in TOOL_INPUT_SCHEMAS
    assert "merge_pr" not in TOOL_INPUT_SCHEMAS
    assert "get_current_time" not in TOOL_INPUT_SCHEMAS
    assert "google_search_grounding_tool" not in TOOL_INPUT_SCHEMAS
