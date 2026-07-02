"""Agent core with canonical event routing, tool schemas, policy gates, and dry-run support.

This module provides the decision-making and execution framework for the webhook
agent. It accepts canonical event objects (produced by the worker's event router),
plans tool calls (via Gemma 4 or a rule-based fallback), validates them, and
executes them behind policy gates.

Design goals:
- Canonical event → structured tool calls
- Trace ID propagation
- Dry-run mode
- Policy gate: automated mutations only when ALLOW_AUTOMATED_MUTATIONS=1
- Writeback policy: block self-referential loops
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .gemma_planner import GemmaPlanner

logger = logging.getLogger("agent_core")

# ---------------------------------------------------------------------------
# Bot identity — used for writeback policy
# ---------------------------------------------------------------------------
BOT_LOGIN = "hannibal-hub-agents[bot]"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class ActionResult:
    tool: str
    success: bool
    detail: str


@dataclass
class CanonicalEvent:
    """A normalized, routed event ready for planning.

    Produced by the worker after route_event() and loop-avoidance checks.
    """

    canonical: str  # e.g. "pull_request.opened"
    delivery_id: str
    event_name: str
    action: str | None
    sender: dict[str, Any] | None
    installation: dict[str, Any] | None
    repository: dict[str, Any] | None
    raw_payload: dict[str, Any]


class ToolValidationError(Exception):
    pass


def generate_trace_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Tool validators
# ---------------------------------------------------------------------------
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
        isinstance(label, str) for label in args["labels"]
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


def validate_reply_to_review_comment_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("pr_number"), int):
        raise ToolValidationError("reply_to_review_comment.pr_number must be an int")
    if not isinstance(args.get("comment_id"), int):
        raise ToolValidationError("reply_to_review_comment.comment_id must be an int")
    if not isinstance(args.get("body"), str) or not args["body"]:
        raise ToolValidationError(
            "reply_to_review_comment.body must be a non-empty string"
        )


def validate_submit_review_args(args: dict[str, Any]) -> None:
    if not isinstance(args.get("pr_number"), int):
        raise ToolValidationError("submit_review.pr_number must be an int")
    if not isinstance(args.get("body"), str) or not args["body"]:
        raise ToolValidationError("submit_review.body must be a non-empty string")
    event = args.get("event", "COMMENT")
    if event not in ("APPROVE", "COMMENT", "REQUEST_CHANGES"):
        raise ToolValidationError(
            "submit_review.event must be one of APPROVE|COMMENT|REQUEST_CHANGES"
        )


# ---------------------------------------------------------------------------
# Writeback policy
# ---------------------------------------------------------------------------
def check_writeback_policy(
    event: CanonicalEvent,
    action: dict[str, Any],
) -> str | None:
    """Check if a tool action should be blocked by writeback policy.

    Returns None if allowed, or a string reason if blocked.
    """
    sender = event.sender or {}
    sender_login = sender.get("login", "")

    # Block self-referential loops: if the event sender is the bot, block
    # all mutations unless the canonical event is in the allowlist.
    if sender_login == BOT_LOGIN:
        allowed_followups: set[str] = set()
        if event.canonical not in allowed_followups:
            return (
                f"writeback policy: bot-authored event '{event.canonical}' blocked "
                f"— only deliberate follow-ups are allowed"
            )

    # Block mutations on events that should never trigger writebacks
    read_only_events: set[str] = {
        "pull_request.synchronize",
        "pull_request.closed",
        "label.deleted",
        "installation.created",
        "installation.deleted",
        "installation.suspend",
        "installation.unsuspend",
        "ping",
        "unknown",
    }
    if event.canonical in read_only_events:
        return (
            f"writeback policy: event '{event.canonical}' is read-only, "
            f"no mutations allowed"
        )

    return None


# ---------------------------------------------------------------------------
# Agent Core
# ---------------------------------------------------------------------------
class AgentCore:
    def __init__(
        self, gh_client, dry_run: bool = False, planner: GemmaPlanner | None = None
    ):
        self.gh = gh_client
        self.dry_run = dry_run
        self.planner = planner or GemmaPlanner.from_env()

    # ------------------------------------------------------------------
    # Canonical event → tool calls (planning)
    # ------------------------------------------------------------------
    def decide(
        self, event: CanonicalEvent, trace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Map a canonical event to a list of tool calls.

        If Gemma 4 is configured, ask it to plan the tool calls. Otherwise,
        fall back to the rule-based planner with event-specific branches.
        """
        trace_id = trace_id or generate_trace_id()

        if self.planner:
            planned = self.planner.plan(event, trace_id)
            return [
                {"tool": p.tool, "args": p.args, "call_id": p.call_id} for p in planned
            ]

        # Rule-based fallback planner
        return self._rule_based_plan(event, trace_id)

    def _rule_based_plan(
        self, event: CanonicalEvent, trace_id: str
    ) -> list[dict[str, Any]]:
        """Rule-based planning for each canonical event type.

        This is the fallback when Gemma 4 is not configured. It provides
        sensible defaults for each event class.
        """
        canonical = event.canonical
        raw = event.raw_payload
        actions: list[dict[str, Any]] = []

        # --- pull_request.opened ---
        if canonical == "pull_request.opened":
            pr = raw.get("pull_request", {})
            pr_number = pr.get("number")
            if pr_number:
                actions.append(
                    {
                        "tool": "add_comment",
                        "args": {
                            "issue_number": pr_number,
                            "body": (
                                "Thanks for opening this pull request! "
                                "The bot will review it shortly."
                            ),
                        },
                    }
                )

        # --- pull_request.synchronize ---
        elif canonical == "pull_request.synchronize":
            # Read-only: log but don't mutate
            logger.info("trace=%s PR synchronize — no action taken", trace_id)

        # --- issue_comment.created ---
        elif canonical == "issue_comment.created":
            issue = raw.get("issue", {})
            issue_number = issue.get("number")
            comment_body = raw.get("comment", {}).get("body", "")
            if issue_number and comment_body:
                # Check for trigger keywords
                lower_body = comment_body.lower()
                if "/review" in lower_body or "/analyze" in lower_body:
                    actions.append(
                        {
                            "tool": "add_comment",
                            "args": {
                                "issue_number": issue_number,
                                "body": (
                                    "I'll analyze this issue and provide feedback shortly."
                                ),
                            },
                        }
                    )

        # --- pull_request_review_comment.created ---
        elif canonical == "pull_request_review_comment.created":
            pr_number = raw.get("pull_request", {}).get("number")
            comment_body = raw.get("comment", {}).get("body", "")
            if pr_number and comment_body:
                lower_body = comment_body.lower()
                if "/review" in lower_body or "/analyze" in lower_body:
                    actions.append(
                        {
                            "tool": "add_review_comment",
                            "args": {
                                "pr_number": pr_number,
                                "body": (
                                    "I'll review this PR and provide feedback shortly."
                                ),
                            },
                        }
                    )

        # --- pull_request_review.submitted ---
        elif canonical == "pull_request_review.submitted":
            logger.info("trace=%s review submitted — no automatic follow-up", trace_id)

        # --- pull_request_review_requested ---
        elif canonical == "pull_request_review_requested":
            pr_number = raw.get("pull_request", {}).get("number")
            if pr_number:
                actions.append(
                    {
                        "tool": "add_comment",
                        "args": {
                            "issue_number": pr_number,
                            "body": ("Review requested! I'll take a look at this PR."),
                        },
                    }
                )

        # --- label.* events ---
        elif canonical.startswith("label."):
            logger.info("trace=%s label event — no automatic action", trace_id)

        # --- installation.* events ---
        elif canonical.startswith("installation."):
            logger.info("trace=%s installation event — no automatic action", trace_id)

        # --- ping ---
        elif canonical == "ping":
            logger.info("trace=%s ping received — no action needed", trace_id)

        # --- unknown ---
        else:
            logger.info(
                "trace=%s unknown canonical event=%s — no action", trace_id, canonical
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

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_action(self, action: dict[str, Any]) -> None:
        tool = action.get("tool")
        args = action.get("args") or {}
        validators = {
            "create_issue": validate_create_issue_args,
            "add_comment": validate_add_comment_args,
            "create_branch_commit": validate_create_branch_commit_args,
            "open_pr": validate_open_pr_args,
            "add_review_comment": validate_add_review_comment_args,
            "merge_pr": validate_merge_pr_args,
            "add_label": validate_add_label_args,
            "assign_reviewers": validate_assign_reviewers_args,
            "reply_to_review_comment": validate_reply_to_review_comment_args,
            "submit_review": validate_submit_review_args,
        }
        validator = validators.get(tool)
        if validator is None:
            raise ToolValidationError(f"unknown tool: {tool}")
        validator(args)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
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
                try:
                    review = pr.create_review(body=args["body"])
                    detail = getattr(review, "html_url", str(review))
                except Exception:
                    comment = pr.create_issue_comment(body=args["body"])
                    detail = getattr(comment, "html_url", str(comment))
                return ActionResult(
                    tool=tool, success=True, detail=f"reviewed/commented: {detail}"
                )
            elif tool == "reply_to_review_comment":
                pr = repo.get_pull(args["pr_number"])
                # PyGitHub doesn't have a direct reply method; use issue comment as fallback
                comment = pr.create_issue_comment(body=args["body"])
                return ActionResult(
                    tool=tool,
                    success=True,
                    detail=f"replied to review comment: {comment.html_url}",
                )
            elif tool == "submit_review":
                pr = repo.get_pull(args["pr_number"])
                review = pr.create_review(
                    body=args["body"],
                    event=args.get("event", "COMMENT"),
                )
                detail = getattr(review, "html_url", str(review))
                return ActionResult(
                    tool=tool, success=True, detail=f"submitted review: {detail}"
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

    # ------------------------------------------------------------------
    # Run (top-level entry point)
    # ------------------------------------------------------------------
    def run(
        self,
        event_data: dict[str, Any],
        repo_full_name: str,
        trace_id: str | None = None,
    ) -> list[ActionResult]:
        """Process a normalized event through the full agent pipeline.

        1. Build a CanonicalEvent from the normalized data
        2. Check writeback policy
        3. Plan tool calls
        4. Execute each tool call behind policy gates
        """
        trace_id = trace_id or generate_trace_id()

        # Build canonical event from normalized data
        canonical = event_data.get("canonical", "") or self._infer_canonical(event_data)
        event = CanonicalEvent(
            canonical=canonical,
            delivery_id=event_data.get("delivery_id", "unknown"),
            event_name=event_data.get("event_name", "unknown"),
            action=event_data.get("action"),
            sender=event_data.get("sender"),
            installation=event_data.get("installation"),
            repository=event_data.get("repository"),
            raw_payload=event_data.get("raw_payload", {}),
        )

        logger.info(
            "agent run trace=%s canonical=%s repo=%s dry_run=%s",
            trace_id,
            event.canonical,
            repo_full_name,
            self.dry_run,
        )

        # Plan tool calls
        actions = self.decide(event, trace_id=trace_id)

        # Execute each action behind writeback policy
        results: list[ActionResult] = []
        for act in actions:
            # Writeback policy check
            policy_reason = check_writeback_policy(event, act)
            if policy_reason:
                results.append(
                    ActionResult(
                        tool=act["tool"],
                        success=False,
                        detail=policy_reason,
                    )
                )
                logger.info(
                    "writeback blocked trace=%s tool=%s reason=%s",
                    trace_id,
                    act["tool"],
                    policy_reason,
                )
                continue

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

    @staticmethod
    def _infer_canonical(event_data: dict[str, Any]) -> str:
        """Infer a canonical event string from normalized data if not already set."""
        event_name = event_data.get("event_name", "")
        action = event_data.get("action") or ""
        if action:
            return f"{event_name}.{action}"
        return event_name


def example_usage():
    # This function is illustrative and not called in production by the worker.
    from github import Auth, Github

    gh = Github(auth=Auth.Token("fake-token"))
    core = AgentCore(gh_client=gh, dry_run=True)
    ev = {
        "delivery_id": "test-123",
        "event_name": "pull_request",
        "action": "opened",
        "canonical": "pull_request.opened",
        "sender": {"login": "test-user"},
        "repository": {"full_name": "owner/repo"},
        "raw_payload": {
            "pull_request": {"number": 1},
            "action": "opened",
        },
    }
    print(core.run(ev, "owner/repo"))


if __name__ == "__main__":
    example_usage()
