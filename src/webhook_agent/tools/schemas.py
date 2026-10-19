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


class ListDirInput(ToolInputBase):
    """Input schema for list_dir tool."""

    dir_path: str = Field(
        default="",
        description="Path to the directory in the repository (leave empty for root).",
    )
    ref: str | None = Field(default=None, description="Branch name, tag, or commit SHA.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "dir_path" not in data:
                if "path" in data:
                    data["dir_path"] = data["path"]
                elif "directory" in data:
                    data["dir_path"] = data["directory"]
        return data


TOOL_INPUT_SCHEMAS: dict[str, type[ToolInputBase]] = {
    "read_file": ReadFileInput,
    "list_dir": ListDirInput,
    "get_issue": GetIssueInput,
    "review": ReviewInput,
}

__all__ = [
    "TOOL_INPUT_SCHEMAS",
    "GetIssueInput",
    "ListDirInput",
    "ReadFileInput",
    "ReviewInput",
    "ToolInputBase",
]
