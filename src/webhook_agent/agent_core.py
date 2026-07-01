"""Agent core with tool schemas, policy gates, trace IDs, and dry-run support.

This module provides a small framework for deciding on actions (tools) in
response to webhook events and executing them safely behind policy checks.

Design goals:
- Structured tool declarations with simple validation
- Trace ID propagation
- Dry-run mode
- Policy gate: automated mutations are only allowed when ALLOW_AUTOMATED_MUTATIONS=1
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .gemma_planner import GemmaPlanner

logger = logging.getLogger("agent_core")


@dataclass
class ActionResult:
    tool: str
    success: bool
    detail: str


class ToolValidationError(Exception):
    pass


def generate_trace_id() -> str:
    return uuid.uuid4().hex


def validate_create_issue_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("title"), str) or not args["title"]:
        raise ToolValidationError("create_issue.title must be a non-empty string")
    if "body" in args and not isinstance(args["body"], str):
        raise ToolValidationError("create_issue.body must be a string")


def validate_add_comment_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("issue_number"), int):
        raise ToolValidationError("add_comment.issue_number must be an int")
    if not isinstance(args.get("body"), str) or not args["body"]:
        raise ToolValidationError("add_comment.body must be a non-empty string")


def validate_create_branch_commit_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("branch_name"), str) or not args["branch_name"]:
        raise ToolValidationError(
            "create_branch_commit.branch_name must be a non-empty string"
        )
    if not isinstance(args.get("file_path"), str) or not args["file_path"]:
        raise ToolValidationError(
            "create_branch_commit.file_path must be a non-empty string"
        )
    if not isinstance(args.get("file_content"), str):
        raise ToolValidationError("create_branch_commit.file_content must be a string")


def validate_open_pr_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("head_branch"), str) or not args["head_branch"]:
        raise ToolValidationError("open_pr.head_branch must be a non-empty string")
    if not isinstance(args.get("base_branch"), str) or not args["base_branch"]:
        raise ToolValidationError("open_pr.base_branch must be a non-empty string")
    if not isinstance(args.get("title"), str) or not args["title"]:
        raise ToolValidationError("open_pr.title must be a non-empty string")


def validate_add_review_comment_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("pr_number"), int):
        raise ToolValidationError("add_review_comment.pr_number must be an int")
    if not isinstance(args.get("body"), str) or not args["body"]:
        raise ToolValidationError("add_review_comment.body must be a non-empty string")


def validate_merge_pr_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("pr_number"), int):
        raise ToolValidationError("merge_pr.pr_number must be an int")
    if args.get("merge_method") not in (None, "merge", "squash", "rebase"):
        raise ToolValidationError(
            "merge_pr.merge_method must be one of merge|squash|rebase"
        )


def validate_add_label_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("labels"), list) or not all(
        isinstance(l, str) for l in args["labels"]
    ):
        raise ToolValidationError("add_label.labels must be a list of strings")


def validate_assign_reviewers_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("pr_number"), int):
        raise ToolValidationError("assign_reviewers.pr_number must be an int")
    if not isinstance(args.get("reviewers"), list) or not all(
        isinstance(r, str) for r in args["reviewers"]
    ):
        raise ToolValidationError(
            "assign_reviewers.reviewers must be a list of usernames"
        )


class AgentCore:
    def __init__(
        self, gh_client, dry_run: bool = False, planner: GemmaPlanner | None = None
    ):
        self.gh = gh_client
        self.dry_run = dry_run
        self.planner = planner or GemmaPlanner.from_env()

    def decide(
        self, event: dict[str, Any], trace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Map the event payload to a list of tool calls.

        If Gemma 4 is configured, ask it to plan the tool calls. Otherwise,
        fall back to the small rule-based planner.
        """
        repo_full_name = event.get("repo") or ""
        trace_id = trace_id or event.get("trace_id") or generate_trace_id()

        if self.planner:
            planned = self.planner.plan(event, repo_full_name, trace_id)
            return [
                {"tool": p.tool, "args": p.args, "call_id": p.call_id} for p in planned
            ]

        actions: list[dict[str, Any]] = []
        # If payload asks for writeback_create_issue, prepare a create_issue action
        if event.get("writeback_create_issue"):
            actions.append(
                {
                    "tool": "create_issue",
                    "args": {
                        "title": event.get("writeback_title"),
                        "body": event.get("writeback_body", ""),
                    },
                }
            )

        # Example: add comment if requested
        if event.get("writeback_add_comment"):
            actions.append(
                {
                    "tool": "add_comment",
                    "args": {
                        "issue_number": int(event.get("writeback_issue_number")),
                        "body": event.get("writeback_comment_body"),
                    },
                }
            )

        return actions

    def validate_action(self, action: dict[str, Any]) -> None:
        tool = action.get("tool")
        args = action.get("args") or {}
        if tool == "create_issue":
            validate_create_issue_args(args)
        elif tool == "add_comment":
            validate_add_comment_args(args)
        elif tool == "create_branch_commit":
            validate_create_branch_commit_args(args)
        elif tool == "open_pr":
            validate_open_pr_args(args)
        elif tool == "add_review_comment":
            validate_add_review_comment_args(args)
        elif tool == "merge_pr":
            validate_merge_pr_args(args)
        elif tool == "add_label":
            validate_add_label_args(args)
        elif tool == "assign_reviewers":
            validate_assign_reviewers_args(args)
        else:
            raise ToolValidationError(f"unknown tool: {tool}")

    def execute_action(
        self, repo_full_name: str, action: dict[str, Any], trace_id: str
    ) -> ActionResult:
        tool = action["tool"]
        args = action.get("args") or {}

        # Policy gate
        allow_auto = os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "0") in (
            "1",
            "true",
            "True",
        )
        if not allow_auto and not self.dry_run:
            return ActionResult(
                tool=tool, success=False, detail="mutations are disabled by policy"
            )

        try:
            self.validate_action(action)
        except ToolValidationError as e:
            return ActionResult(
                tool=tool, success=False, detail=f"validation error: {e}"
            )

        if self.dry_run:
            return ActionResult(
                tool=tool,
                success=True,
                detail=f"dry-run: would execute {tool} with {args}",
            )

        # Perform action
        try:
            repo = self.gh.get_repo(repo_full_name)
            if tool == "create_issue":
                issue = repo.create_issue(
                    title=args["title"], body=args.get("body", "")
                )
                return ActionResult(
                    tool=tool,
                    success=True,
                    detail=f"created issue #{issue.number} {issue.html_url}",
                )
            elif tool == "add_comment":
                issue = repo.get_issue(number=args["issue_number"])
                comment = issue.create_comment(body=args["body"])
                return ActionResult(
                    tool=tool, success=True, detail=f"commented: {comment.html_url}"
                )
            elif tool == "create_branch_commit":
                base = args.get("base_branch") or repo.default_branch
                new_branch = args["branch_name"]
                ref = f"refs/heads/{new_branch}"
                created = False
                try:
                    repo.get_branch(new_branch)
                except Exception:
                    sb = repo.get_branch(base)
                    repo.create_git_ref(ref=ref, sha=sb.commit.sha)
                    created = True
                path = args["file_path"]
                content = args["file_content"]
                try:
                    repo.create_file(
                        path, f"Add {path} via agent", content, branch=new_branch
                    )
                except Exception:
                    existing = repo.get_contents(path, ref=new_branch)
                    repo.update_file(
                        path,
                        f"Update {path} via agent",
                        content,
                        existing.sha,
                        branch=new_branch,
                    )
                return ActionResult(
                    tool=tool,
                    success=True,
                    detail=f"branch {new_branch} prepared (created={created})",
                )
            elif tool == "open_pr":
                pr = repo.create_pull(
                    title=args["title"],
                    body=args.get("body", ""),
                    head=args["head_branch"],
                    base=args["base_branch"],
                )
                return ActionResult(
                    tool=tool,
                    success=True,
                    detail=f"opened PR #{pr.number} {pr.html_url}",
                )
            elif tool == "add_review_comment":
                pr = repo.get_pull(args["pr_number"])
                # Use review creation if available, otherwise fallback to issue comment
                try:
                    review = pr.create_review(
                        body=args["body"]
                    )  # may require different args
                    detail = getattr(review, "html_url", str(review))
                except Exception:
                    comment = pr.create_issue_comment(body=args["body"])
                    detail = getattr(comment, "html_url", str(comment))
                return ActionResult(
                    tool=tool, success=True, detail=f"reviewed/commented: {detail}"
                )
            elif tool == "merge_pr":
                pr = repo.get_pull(args["pr_number"])
                method = args.get("merge_method") or "merge"
                res = pr.merge(merge_method=method)
                return ActionResult(tool=tool, success=True, detail=f"merged: {res}")
            elif tool == "add_label":
                issue_number = args.get("issue_number")
                if issue_number:
                    issue = repo.get_issue(number=issue_number)
                    issue.add_to_labels(*args["labels"])
                else:
                    repo.add_to_labels(*args["labels"])
                return ActionResult(
                    tool=tool, success=True, detail=f"labels added: {args['labels']}"
                )
            elif tool == "assign_reviewers":
                pr = repo.get_pull(args["pr_number"])
                pr.create_review_request(reviewers=args["reviewers"])
                return ActionResult(
                    tool=tool,
                    success=True,
                    detail=f"requested reviewers: {args['reviewers']}",
                )
            else:
                return ActionResult(
                    tool=tool, success=False, detail="unknown tool at execution"
                )
        except Exception as exc:
            logger.exception("action execution failed")
            return ActionResult(tool=tool, success=False, detail=str(exc))

    def run(
        self, event: dict[str, Any], repo_full_name: str, trace_id: str | None = None
    ) -> list[ActionResult]:
        trace_id = trace_id or generate_trace_id()
        logger.info(
            "agent run trace=%s event_keys=%s repo=%s dry_run=%s",
            trace_id,
            list(event.keys()),
            repo_full_name,
            self.dry_run,
        )
        actions = self.decide(event, trace_id=trace_id)
        results: list[ActionResult] = []
        for act in actions:
            res = self.execute_action(repo_full_name, act, trace_id)
            results.append(res)
            logger.info(
                "action result trace=%s tool=%s success=%s detail=%s",
                trace_id,
                res.tool,
                res.success,
                res.detail,
            )
        return results


def example_usage():
    # This function is illustrative and not called in production by the worker.
    from github import Auth, Github

    gh = Github(auth=Auth.Token("fake-token"))
    core = AgentCore(gh_client=gh, dry_run=True)
    ev = {
        "writeback_create_issue": True,
        "writeback_title": "test",
        "writeback_body": "hello",
    }
    print(core.run(ev, "owner/repo"))


if __name__ == "__main__":
    example_usage()
