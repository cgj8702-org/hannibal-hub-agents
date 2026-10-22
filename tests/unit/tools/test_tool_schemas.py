"""Unit tests for tool input Pydantic schemas and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from webhook_agent.tools.schemas import (
    TOOL_INPUT_SCHEMAS,
    GetIssueInput,
    ListDirInput,
    ReadFileInput,
    ReviewInput,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_read_file_input():
    # Valid
    inp = ReadFileInput.model_validate({"file_path": "src/main.py", "ref": "feat-branch"})
    assert inp.file_path == "src/main.py"
    assert inp.ref == "feat-branch"

    # start_line and max_lines accepted
    inp2 = ReadFileInput.model_validate(
        {"file_path": "src/main.py", "start_line": 42, "max_lines": 10}
    )
    assert inp2.file_path == "src/main.py"
    assert inp2.start_line == 42
    assert inp2.max_lines == 10

    # Missing file_path
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({})

    # Empty file_path
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({"file_path": ""})

    # Negative start_line rejected
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({"file_path": "src/main.py", "start_line": 0})

    # Negative max_lines rejected
    with pytest.raises(ValidationError):
        ReadFileInput.model_validate({"file_path": "src/main.py", "max_lines": -1})

    # Aliases coerced
    inp3 = ReadFileInput.model_validate({"file_path": "src/main.py", "start": "5", "limit": "20"})
    assert inp3.start_line == 5
    assert inp3.max_lines == 20

    # startLine / maxLines aliases
    inp4 = ReadFileInput.model_validate(
        {"file_path": "src/main.py", "startLine": 100, "maxLines": 3}
    )
    assert inp4.start_line == 100
    assert inp4.max_lines == 3

    # from_line / lines aliases
    inp5 = ReadFileInput.model_validate({"file_path": "src/main.py", "from_line": 7, "lines": 50})
    assert inp5.start_line == 7
    assert inp5.max_lines == 50


def test_list_dir_input():
    # Valid default (root)
    inp1 = ListDirInput.model_validate({})
    assert inp1.dir_path == ""
    assert inp1.ref is None

    # Valid with path
    inp2 = ListDirInput.model_validate({"dir_path": "src/webhook_agent", "ref": "main"})
    assert inp2.dir_path == "src/webhook_agent"
    assert inp2.ref == "main"

    # Coerced from path alias
    inp3 = ListDirInput.model_validate({"path": "tests"})
    assert inp3.dir_path == "tests"

    # Coerced from directory alias
    inp4 = ListDirInput.model_validate({"directory": "docs"})
    assert inp4.dir_path == "docs"


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


def test_tool_input_schemas_registry():
    assert "read_file" in TOOL_INPUT_SCHEMAS
    assert "list_dir" in TOOL_INPUT_SCHEMAS
    assert "get_issue" in TOOL_INPUT_SCHEMAS
    assert "review" in TOOL_INPUT_SCHEMAS
    assert "create_issue" in TOOL_INPUT_SCHEMAS
    assert "get_commit_diff" not in TOOL_INPUT_SCHEMAS
    assert "add_label" not in TOOL_INPUT_SCHEMAS
    assert "update_issue" not in TOOL_INPUT_SCHEMAS
    assert "merge_pr" not in TOOL_INPUT_SCHEMAS
    assert "get_current_time" not in TOOL_INPUT_SCHEMAS
    assert "google_search_grounding_tool" not in TOOL_INPUT_SCHEMAS
