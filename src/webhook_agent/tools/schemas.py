"""Pydantic input validation schemas for all model-callable tools.

These schemas are enforced in `before_tool_callback` to guarantee type safety,
coercion (e.g. string integers -> int, field aliasing), and early failure before
touching external GitHub APIs.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from webhook_agent.review.schemas import CodeReviewResponse, SyncReviewResponse


class ToolInputBase(BaseModel):
    """Base class for all tool input validation models."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                (k.strip().strip("'\"").strip() if isinstance(k, str) else k): v
                for k, v in data.items()
            }
        return data


class ReadFileInput(ToolInputBase):
    """Input schema for read_file tool."""

    file_path: str = Field(..., min_length=1, description="Path to the file in the repository.")
    ref: str | None = Field(default=None, description="Branch name, tag, or commit SHA.")
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="1-based line number to start reading from.",
    )
    max_lines: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of lines to return.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "start_line" not in data or data["start_line"] is None:
                for key in ("start", "startLine", "from_line"):
                    if data.get(key) is not None:
                        with contextlib.suppress(ValueError, TypeError):
                            data["start_line"] = int(data[key])
                        break
            if "max_lines" not in data or data["max_lines"] is None:
                for key in ("limit", "lines", "maxLines", "n_lines"):
                    if data.get(key) is not None:
                        with contextlib.suppress(ValueError, TypeError):
                            data["max_lines"] = int(data[key])
                        break
        return data


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
    event: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"] | None = Field(
        default=None, description="Review verdict event."
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
                elif "pr" in data:
                    data["pr_number"] = data["pr"]

            if "body" not in data or not data["body"]:
                if data.get("report"):
                    if isinstance(data["report"], dict):
                        data["body"] = json.dumps(data["report"])
                    elif isinstance(data["report"], str):
                        data["body"] = data["report"]
                elif any(
                    k in data
                    for k in (
                        "scorecard",
                        "executive_summary",
                        "summary",
                        "critical_issues",
                        "minor_suggestions",
                        "resolutions",
                    )
                ):
                    payload = {
                        k: v
                        for k, v in data.items()
                        if k not in ("pr_number", "issue_number", "number", "pr", "event")
                    }
                    data["body"] = json.dumps(payload)

            if "event" in data and isinstance(data["event"], str):
                data["event"] = data["event"].upper()
        return data


class SubmitInitialReviewInput(ToolInputBase):
    """Input schema for submit_initial_review tool."""

    pr_number: int = Field(..., ge=1, description="Pull request number.")
    report: CodeReviewResponse | dict[str, Any] = Field(
        ..., description="Structured code review report matching CodeReviewResponse schema."
    )
    event: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"] | None = Field(
        default=None, description="Review verdict event. Defaults to report verdict."
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
                elif "pr" in data:
                    data["pr_number"] = data["pr"]

            if "report" not in data:
                if "body" in data and isinstance(data["body"], dict):
                    data["report"] = data["body"]
                elif any(
                    k in data
                    for k in (
                        "scorecard",
                        "executive_summary",
                        "critical_issues",
                        "minor_suggestions",
                    )
                ):
                    payload = {
                        k: v
                        for k, v in data.items()
                        if k not in ("pr_number", "issue_number", "number", "pr", "event")
                    }
                    data["report"] = payload

            if "event" in data and isinstance(data["event"], str):
                data["event"] = data["event"].upper()
        return data


class SubmitSyncReviewInput(ToolInputBase):
    """Input schema for submit_sync_review tool."""

    pr_number: int = Field(..., ge=1, description="Pull request number.")
    report: SyncReviewResponse | dict[str, Any] = Field(
        ...,
        description="Structured synchronization review report matching SyncReviewResponse schema.",
    )
    event: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"] | None = Field(
        default=None, description="Review verdict event. Defaults to report verdict."
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
                elif "pr" in data:
                    data["pr_number"] = data["pr"]

            if "report" not in data:
                if "body" in data and isinstance(data["body"], dict):
                    data["report"] = data["body"]
                elif any(
                    k in data
                    for k in (
                        "scorecard",
                        "summary",
                        "resolutions",
                        "critical_issues",
                        "minor_suggestions",
                    )
                ):
                    payload = {
                        k: v
                        for k, v in data.items()
                        if k not in ("pr_number", "issue_number", "number", "pr", "event")
                    }
                    data["report"] = payload

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


class CreateIssueInput(ToolInputBase):
    """Input schema for create_issue tool."""

    title: str = Field(description="Issue title.")
    body: str = Field(description="Markdown issue description and structured task specification.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "title" not in data and "name" in data:
                data["title"] = data["name"]
            if "body" not in data and "description" in data:
                data["body"] = data["description"]
        return data


class SearchFilesInput(ToolInputBase):
    """Input schema for search_files tool."""

    pattern: str = Field(
        ..., min_length=1, max_length=500, description="Regex pattern to search for."
    )
    path: str = Field(
        default=".", description="Relative path within the environment to search from."
    )
    file_glob: str | None = Field(
        default=None, description="Optional glob pattern to filter filenames (e.g. '*.py')."
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of matches to return.")
    ignore_case: bool = Field(
        default=False, description="Whether to perform case-insensitive regex matching."
    )
    no_ignore: bool = Field(default=False, description="If True, do not skip ignored directories.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "pattern" not in data:
                for key in ("query", "regex", "search", "term"):
                    if data.get(key) is not None:
                        data["pattern"] = str(data[key])
                        break
            if "file_glob" not in data and "glob" in data:
                data["file_glob"] = data["glob"]
        return data


TOOL_INPUT_SCHEMAS: dict[str, type[ToolInputBase]] = {
    "read_file": ReadFileInput,
    "list_dir": ListDirInput,
    "get_issue": GetIssueInput,
    "review": ReviewInput,
    "submit_initial_review": SubmitInitialReviewInput,
    "submit_sync_review": SubmitSyncReviewInput,
    "create_issue": CreateIssueInput,
    "search_files": SearchFilesInput,
}


__all__ = [
    "TOOL_INPUT_SCHEMAS",
    "CreateIssueInput",
    "GetIssueInput",
    "ListDirInput",
    "ReadFileInput",
    "ReviewInput",
    "SearchFilesInput",
    "SubmitInitialReviewInput",
    "SubmitSyncReviewInput",
    "ToolInputBase",
]
