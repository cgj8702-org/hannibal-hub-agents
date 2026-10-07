"""Pydantic input validation schemas for all model-callable tools.

These schemas are enforced in `before_tool_callback` to guarantee type safety,
coercion (e.g. string integers -> int, field aliasing), and early failure before
touching external GitHub APIs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolInputBase(BaseModel):
    """Base class for all tool input validation models."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ReadFileInput(ToolInputBase):
    """Input schema for read_file tool."""

    file_path: str = Field(..., min_length=1, description="Path to the file in the repository.")
    ref: str | None = Field(default=None, description="Branch name, tag, or commit SHA.")


class GetIssueInput(ToolInputBase):
    """Input schema for get_issue tool."""

    number: int = Field(..., ge=1, description="Issue or PR number.")
    include_diff: bool = Field(default=False, description="Whether to include full PR diff.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "number" not in data:
                if "issue_number" in data:
                    data["number"] = data["issue_number"]
                elif "pr_number" in data:
                    data["number"] = data["pr_number"]
        return data


class GetCommitDiffInput(ToolInputBase):
    """Input schema for get_commit_diff tool."""

    base_sha: str = Field(..., min_length=1, description="Base commit SHA.")
    head_sha: str = Field(..., min_length=1, description="Head commit SHA.")


class ReviewInput(ToolInputBase):
    """Input schema for review tool."""

    pr_number: int = Field(..., ge=1, description="Pull request number.")
    body: str = Field(..., min_length=1, description="Review payload JSON or markdown string.")
    event: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"] = Field(
        default="COMMENT", description="Review verdict event."
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "pr_number" not in data:
                if "issue_number" in data:
                    data["pr_number"] = data["issue_number"]
                elif "number" in data:
                    data["pr_number"] = data["number"]
            if "event" in data and isinstance(data["event"], str):
                data["event"] = data["event"].upper()
        return data


class AddLabelInput(ToolInputBase):
    """Input schema for add_label tool."""

    issue_number: int = Field(..., ge=1, description="Issue or PR number.")
    labels: list[str] = Field(..., min_length=1, description="List of label names to attach.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "issue_number" not in data:
                if "pr_number" in data:
                    data["issue_number"] = data["pr_number"]
                elif "number" in data:
                    data["issue_number"] = data["number"]
            if "labels" in data and isinstance(data["labels"], str):
                data["labels"] = [lbl.strip() for lbl in data["labels"].split(",") if lbl.strip()]
        return data


class CreateIssueInput(ToolInputBase):
    """Input schema for create_issue tool."""

    title: str = Field(..., min_length=1, description="Issue title.")
    body: str = Field(..., min_length=1, description="Structured markdown task specification.")
    labels: list[str] | None = Field(default=None, description="Optional list of labels to attach.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "labels" in data and isinstance(data["labels"], str):
                data["labels"] = [lbl.strip() for lbl in data["labels"].split(",") if lbl.strip()]
        return data


class UpdateIssueInput(ToolInputBase):
    """Input schema for update_issue tool."""

    issue_number: int = Field(..., ge=1, description="Issue or PR number.")
    title: str | None = Field(default=None, description="Updated issue title.")
    body: str | None = Field(default=None, description="Updated issue body.")
    state: Literal["open", "closed"] | None = Field(default=None, description="Issue state.")
    labels: list[str] | None = Field(default=None, description="Updated list of labels.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "issue_number" not in data:
                if "pr_number" in data:
                    data["issue_number"] = data["pr_number"]
                elif "number" in data:
                    data["issue_number"] = data["number"]
            if "labels" in data and isinstance(data["labels"], str):
                data["labels"] = [lbl.strip() for lbl in data["labels"].split(",") if lbl.strip()]
            if "state" in data and isinstance(data["state"], str):
                data["state"] = data["state"].lower()
        return data


class MergePrInput(ToolInputBase):
    """Input schema for merge_pr tool."""

    pr_number: int = Field(..., ge=1, description="Pull request number.")
    merge_method: Literal["merge", "squash", "rebase"] = Field(
        default="merge", description="Merge method to use."
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "pr_number" not in data:
                if "issue_number" in data:
                    data["pr_number"] = data["issue_number"]
                elif "number" in data:
                    data["pr_number"] = data["number"]
            if "merge_method" in data and isinstance(data["merge_method"], str):
                data["merge_method"] = data["merge_method"].lower()
        return data


TOOL_INPUT_SCHEMAS: dict[str, type[ToolInputBase]] = {
    "read_file": ReadFileInput,
    "get_issue": GetIssueInput,
    "get_commit_diff": GetCommitDiffInput,
    "review": ReviewInput,
    "add_label": AddLabelInput,
    "create_issue": CreateIssueInput,
    "update_issue": UpdateIssueInput,
    "merge_pr": MergePrInput,
}

__all__ = [
    "TOOL_INPUT_SCHEMAS",
    "AddLabelInput",
    "CreateIssueInput",
    "GetCommitDiffInput",
    "GetIssueInput",
    "MergePrInput",
    "ReadFileInput",
    "ReviewInput",
    "ToolInputBase",
    "UpdateIssueInput",
]
