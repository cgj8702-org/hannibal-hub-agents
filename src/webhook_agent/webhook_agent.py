"""ADK-powered webhook agent that replaces the Gemma planner.

This module defines the ADK agent with all GitHub tools as Python functions,
and provides a synchronous interface for the existing webhook pipeline.

The agent uses:
- Gemma-4-31b-it via ADK's Gemini model wrapper
- ChromaDBMemoryService for persistent, searchable conversation memory
- InMemorySessionService for per-PR conversation context
- Plain Python functions as tools (ADK auto-generates JSON schemas)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from github import Github
from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from .chroma_memory_service import ChromaDBMemoryService


@dataclass
class ActionResult:
    """Result of executing a single agent tool."""

    tool: str
    success: bool
    detail: str


logger = logging.getLogger("webhook_agent")

# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

# ---------------------------------------------------------------------------
# ADK Tool Functions
# Each function becomes an ADK tool automatically. The docstring and type
# hints define the JSON schema that Gemma sees.
# ---------------------------------------------------------------------------


def _get_gh_from_ctx(ctx: Context) -> Github:
    """Retrieve the Github client from the agent context."""
    gh = ctx.user_context.get("gh_client")
    if gh is None:
        raise RuntimeError("GitHub client not found in agent context")
    return gh


def _get_repo_full_name(ctx: Context) -> str:
    """Retrieve the repo full name from the agent context."""
    name = ctx.user_context.get("repo_full_name")
    if name is None:
        raise RuntimeError("repo_full_name not found in agent context")
    return name


def add_comment(ctx: Context, issue_number: int, body: str) -> str:
    """Add a general comment to an issue or pull request.

    Args:
        issue_number: Issue or PR number.
        body: Comment body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        comment = issue.create_comment(body=body)
        return f"Commented: {comment.html_url}"
    except Exception as e:
        return f"Error adding comment: {e}"


def add_label(ctx: Context, labels: list[str], issue_number: int | None = None) -> str:
    """Add labels to an issue, PR, or repository.

    Args:
        labels: List of label names to add.
        issue_number: Optional issue/PR number. If omitted, adds to the repo.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        if issue_number:
            issue = repo.get_issue(number=issue_number)
            issue.add_to_labels(*labels)
        else:
            repo.add_to_labels(*labels)
        return f"Labels added: {labels}"
    except Exception as e:
        return f"Error adding labels: {e}"


def add_review_comment(ctx: Context, pr_number: int, body: str) -> str:
    """Leave a general review-style comment or fallback comment on a PR.

    Args:
        pr_number: Pull request number.
        body: Review comment body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        try:
            review = pr.create_review(body=body)
            detail = getattr(review, "html_url", str(review))
        except Exception:
            comment = pr.create_issue_comment(body=body)
            detail = getattr(comment, "html_url", str(comment))
        return f"Reviewed/commented: {detail}"
    except Exception as e:
        return f"Error adding review comment: {e}"


def reply_to_review_comment(
    ctx: Context, pr_number: int, comment_id: int, body: str
) -> str:
    """Reply to a specific review comment on a pull request.

    Args:
        pr_number: Pull request number.
        comment_id: Review comment ID to reply to.
        body: Reply body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)

        # Try to find the comment in PR reviews for a threaded reply
        target_comment = None
        try:
            for review in pr.get_reviews():
                for comment in review.comments:
                    if comment.id == comment_id:
                        target_comment = comment
                        break
                if target_comment:
                    break
        except Exception:
            logger.debug("Failed to iterate reviews for comment %s", comment_id)

        if target_comment:
            reply = target_comment.create_comment(body)
            return f"Replied to review comment: {reply.html_url}"

        # Fallback: check if it's a general PR comment
        try:
            issue = repo.get_issue(number=pr_number)
            comment = issue.get_comment(comment_id)
            reply = issue.create_comment(body=f"Re: {comment.body[:100]}...\n\n{body}")
            return f"Replied to issue comment: {reply.html_url}"
        except Exception:
            return f"Could not find comment {comment_id} to reply to"
    except Exception as e:
        return f"Error replying to review comment: {e}"


def submit_review(
    ctx: Context,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
) -> str:
    """Submit a formal review on a pull request with an event type.

    Args:
        pr_number: Pull request number.
        body: Review body (Markdown).
        event: Review event type. One of APPROVE, COMMENT, REQUEST_CHANGES.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        review = pr.create_review(body=body, event=event)
        detail = getattr(review, "html_url", str(review))
        return f"Submitted review: {detail}"
    except Exception as e:
        return f"Error submitting review: {e}"


def assign_reviewers(ctx: Context, pr_number: int, reviewers: list[str]) -> str:
    """Request reviewers on a pull request.

    Args:
        pr_number: Pull request number.
        reviewers: List of GitHub usernames to request as reviewers.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        pr.create_review_request(reviewers=reviewers)
        return f"Requested reviewers: {reviewers}"
    except Exception as e:
        return f"Error assigning reviewers: {e}"


def open_pr(
    ctx: Context,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str = "",
) -> str:
    """Open a new pull request.

    Args:
        head_branch: Head branch name.
        base_branch: Base branch name.
        title: Pull request title.
        body: Pull request body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
        )
        return f"Opened PR #{pr.number} {pr.html_url}"
    except Exception as e:
        return f"Error opening PR: {e}"


def merge_pr(ctx: Context, pr_number: int, merge_method: str = "merge") -> str:
    """Merge a pull request.

    Args:
        pr_number: Pull request number.
        merge_method: Merge method. One of merge, squash, rebase.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        res = pr.merge(merge_method=merge_method)
        return f"Merged: {res}"
    except Exception as e:
        return f"Error merging PR: {e}"


def create_branch_commit(
    ctx: Context,
    branch_name: str,
    file_path: str,
    file_content: str,
    base_branch: str | None = None,
) -> str:
    """Create a new branch (from base or default) and add or update a file.

    Args:
        branch_name: New branch name.
        file_path: File path to create or update.
        file_content: File content.
        base_branch: Base branch (defaults to repo default).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        base = base_branch or repo.default_branch
        ref = f"refs/heads/{branch_name}"
        created = False
        try:
            repo.get_branch(branch_name)
        except Exception:
            sb = repo.get_branch(base)
            repo.create_git_ref(ref=ref, sha=sb.commit.sha)
            created = True

        try:
            repo.create_file(
                file_path,
                f"Add {file_path} via agent",
                file_content,
                branch=branch_name,
            )
        except Exception:
            existing = repo.get_contents(file_path, ref=branch_name)
            repo.update_file(
                file_path,
                f"Update {file_path} via agent",
                file_content,
                existing.sha,
                branch=branch_name,
            )
        return f"Branch {branch_name} prepared (created={created})"
    except Exception as e:
        return f"Error creating branch/commit: {e}"


def get_pr_diff(ctx: Context, pr_number: int) -> str:
    """Fetch the file diffs and changed files in a pull request.

    Args:
        pr_number: Pull request number.

    Returns:
        A string containing the diff summary.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        files = pr.get_files()
        diff_summary = []
        for f in files:
            diff_summary.append(
                f"File: {f.filename} ({f.status})\nPatch:\n{f.patch}\n{'-' * 40}"
            )
        return "\n".join(diff_summary) if diff_summary else "No files changed."
    except Exception as e:
        return f"Error fetching PR diff: {e}"


def update_pr_description(
    ctx: Context,
    pr_number: int,
    body: str | None = None,
    title: str | None = None,
    ready_for_review: bool | None = None,
) -> str:
    """Update a pull request's description, title, or mark it as ready for review.

    Args:
        pr_number: Pull request number.
        body: New pull request description (Markdown).
        title: Optional new pull request title.
        ready_for_review: Set to true to transition draft PRs to ready-for-review.

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        edit_kwargs: dict[str, Any] = {}
        if body is not None:
            edit_kwargs["body"] = body
        if title is not None:
            edit_kwargs["title"] = title
        if edit_kwargs:
            pr.edit(**edit_kwargs)
        if ready_for_review:
            pr.add_to_labels("ready for review")
        return f"Updated PR #{pr.number}"
    except Exception as e:
        return f"Error updating PR description: {e}"


def create_issue(ctx: Context, title: str, body: str = "") -> str:
    """Create a new issue in the repository.

    Args:
        title: Issue title.
        body: Issue body (Markdown).

    Returns:
        A string describing the result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.create_issue(title=title, body=body)
        return f"Created issue #{issue.number} {issue.html_url}"
    except Exception as e:
        return f"Error creating issue: {e}"


# ---------------------------------------------------------------------------
# System instruction for the agent
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """You are the planner for a GitHub App agent. Your role is to decide which tool(s) to call based on the incoming webhook event.

Rules:
1. Only call tools from the provided set. Do NOT invent tools or parameters.
2. When a real user comments on an issue or pull request, you MUST respond. The user is engaging with the agent — treat this as a conversation and reply appropriately. Do NOT treat user comments as 'no action needed' simply because they lack explicit commands like /review.
3. Keep arguments concise and correct.
4. For PR review events, prefer submit_review over add_review_comment when a formal review is appropriate.
5. The bot's GitHub login is 'hannibal-hub-agents[bot]'. Only this account is the agent itself. All other senders (including 'cgj8702-agents') are real users and should be responded to normally.
6. If no action is needed, respond in text explaining why.
"""

# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


class WebhookAgent:
    """ADK-powered agent for processing GitHub webhook events.

    Wraps the ADK Agent and Runner to provide a synchronous interface
    compatible with the existing webhook pipeline.
    """

    def __init__(
        self,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._app_name = "hannibal-hub-agents"

        # Session service — keeps per-PR conversation history
        self._session_service = InMemorySessionService()

        # Memory service — persistent ChromaDB-backed long-term memory
        self._memory_service = ChromaDBMemoryService()

        # Create the ADK agent with all tools
        self._agent = Agent(
            name="webhook_agent",
            model=Gemini(
                model=os.environ.get("GEMMA_MODEL", "gemma-4-31b-it"),
            ),
            instruction=SYSTEM_INSTRUCTION,
            tools=[
                add_comment,
                add_label,
                add_review_comment,
                reply_to_review_comment,
                submit_review,
                assign_reviewers,
                open_pr,
                merge_pr,
                create_branch_commit,
                get_pr_diff,
                update_pr_description,
                create_issue,
            ],
        )

        # Create the runner
        self._runner = Runner(
            agent=self._agent,
            app_name=self._app_name,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _derive_session_id(self, event_data: dict[str, Any]) -> str:
        """Derive a session ID from the event data for conversation continuity.

        Uses repo_full_name + issue/PR number so that follow-up comments
        on the same thread share a session.
        """
        repo = event_data.get("repository", {})
        repo_name = repo.get("full_name", "unknown")
        raw = event_data.get("raw_payload", {})
        issue = raw.get("issue", {})
        pr = raw.get("pull_request", {})
        number = issue.get("number") or pr.get("number")
        if number:
            return f"{repo_name}/{number}"
        return f"{repo_name}/{event_data.get('delivery_id', 'unknown')}"

    def _build_user_message(self, event_data: dict[str, Any]) -> genai_types.Content:
        """Build a user message from the webhook event data."""
        canonical = event_data.get("canonical", "unknown")
        sender = event_data.get("sender", {})
        sender_login = sender.get("login", "unknown")
        raw = event_data.get("raw_payload", {})

        # Build context from the event
        parts: list[str] = [
            f"Canonical Event: {canonical}",
            f"Sender: {sender_login}",
        ]

        # Add event-specific context
        if canonical.startswith("issue_comment."):
            comment = raw.get("comment", {})
            issue = raw.get("issue", {})
            comment_body = (comment.get("body") or "")[:500]
            parts.append(f"Issue/PR Number: {issue.get('number', 'unknown')}")
            parts.append(f"Comment: {comment_body}")
        elif canonical.startswith("pull_request."):
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"PR Title: {pr.get('title', 'N/A')}")
            parts.append(f"PR Body: {(pr.get('body') or '')[:500]}")
        elif canonical.startswith("pull_request_review_comment."):
            comment = raw.get("comment", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review Comment: {(comment.get('body') or '')[:500]}")
        elif canonical.startswith("pull_request_review."):
            review = raw.get("review", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review: {(review.get('body') or '')[:500]}")

        # Include PR diff if available
        if "pr_diff" in raw:
            parts.append(f"\nPR Diff:\n{raw['pr_diff']}")

        text = "\n".join(parts)
        return genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=text)],
        )

    def plan_and_execute(
        self,
        event_data: dict[str, Any],
        gh_client: Github,
        trace_id: str,
    ) -> list[ActionResult]:
        """Process a webhook event through the ADK agent.

        This is the main entry point, called from agent_core.run().

        Args:
            event_data: Normalized webhook event data.
            gh_client: Authenticated GitHub client.
            trace_id: Trace ID for logging.

        Returns:
            List of ActionResult objects.
        """
        repo_full_name = (
            event_data.get("repository", {}).get("full_name")
            if event_data.get("repository")
            else "unknown"
        )

        # Check writeback policy for bot-authored events
        sender = event_data.get("sender") or {}
        sender_login = sender.get("login", "")
        canonical = event_data.get("canonical", "")

        if sender_login == BOT_LOGIN:
            logger.info(
                "writeback blocked: bot-authored event '%s' (trace: %s)",
                canonical,
                trace_id,
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"writeback policy: bot-authored event '{canonical}' blocked",
                )
            ]

        # Check read-only events
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
        if canonical in read_only_events:
            logger.info(
                "writeback policy: event '%s' is read-only (trace: %s)",
                canonical,
                trace_id,
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"writeback policy: event '{canonical}' is read-only",
                )
            ]

        # Check mutation policy
        allow_auto = os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "0") in (
            "1",
            "true",
            "True",
        )
        if not allow_auto and not self.dry_run:
            logger.info(
                "mutations disabled by policy (trace: %s)",
                trace_id,
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail="mutations are disabled by policy",
                )
            ]

        if self.dry_run:
            logger.info("dry-run mode (trace: %s)", trace_id)
            return [
                ActionResult(
                    tool="plan",
                    success=True,
                    detail="dry-run: would process event through ADK agent",
                )
            ]

        # Derive session and user IDs
        session_id = self._derive_session_id(event_data)
        user_id = sender_login or "anonymous"

        # Build the user message
        user_message = self._build_user_message(event_data)

        # Run the agent asynchronously
        results: list[ActionResult] = []

        async def _run():
            nonlocal results
            try:
                async for event in self._runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=user_message,
                ):
                    # Handle function call events — these are tool invocations
                    if (
                        event.content
                        and event.content.parts
                        and any(
                            hasattr(p, "function_call") and p.function_call
                            for p in event.content.parts
                        )
                    ):
                        for part in event.content.parts:
                            if hasattr(part, "function_call") and part.function_call:
                                fc = part.function_call
                                tool_name = fc.name
                                args = dict(fc.args) if fc.args else {}

                                # Execute the tool via the existing GitHub client
                                result = self._execute_tool(
                                    tool_name,
                                    args,
                                    gh_client,
                                    repo_full_name,
                                    trace_id,
                                )
                                results.append(result)

                    # Handle text responses — log the agent's reasoning
                    if (
                        event.content
                        and event.content.parts
                        and any(
                            hasattr(p, "text") and p.text for p in event.content.parts
                        )
                    ):
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                logger.info(
                                    "🧠 Agent response: %s (trace: %s)",
                                    part.text[:200],
                                    trace_id,
                                )

            except Exception as e:
                logger.exception(
                    "ADK agent run failed (trace: %s): %s",
                    trace_id,
                    e,
                )
                results.append(
                    ActionResult(
                        tool="plan",
                        success=False,
                        detail=f"ADK agent error: {e}",
                    )
                )

        asyncio.run(_run())

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id,
            )

        return results

    def _execute_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        gh_client: Github,
        repo_full_name: str,
        trace_id: str,
    ) -> ActionResult:
        """Execute a single tool call from the ADK agent.

        This mirrors the execution logic from agent_core.execute_action().
        """
        logger.info(
            "🛠️  Executing tool %s for repo %s (trace: %s)",
            tool_name,
            repo_full_name,
            trace_id,
        )

        try:
            repo = gh_client.get_repo(repo_full_name)

            if tool_name == "add_comment":
                issue = repo.get_issue(number=args["issue_number"])
                comment = issue.create_comment(body=args["body"])
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"commented: {comment.html_url}",
                )

            elif tool_name == "add_label":
                issue_number = args.get("issue_number")
                if issue_number:
                    issue = repo.get_issue(number=issue_number)
                    issue.add_to_labels(*args["labels"])
                else:
                    repo.add_to_labels(*args["labels"])
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"labels added: {args['labels']}",
                )

            elif tool_name == "add_review_comment":
                pr = repo.get_pull(args["pr_number"])
                try:
                    review = pr.create_review(body=args["body"])
                    detail = getattr(review, "html_url", str(review))
                except Exception:
                    comment = pr.create_issue_comment(body=args["body"])
                    detail = getattr(comment, "html_url", str(comment))
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"reviewed/commented: {detail}",
                )

            elif tool_name == "reply_to_review_comment":
                pr = repo.get_pull(args["pr_number"])
                comment_id = args["comment_id"]
                body = args["body"]

                target_comment = None
                try:
                    for review in pr.get_reviews():
                        for comment in review.comments:
                            if comment.id == comment_id:
                                target_comment = comment
                                break
                        if target_comment:
                            break
                except Exception:
                    pass

                if target_comment:
                    reply = target_comment.create_comment(body)
                    return ActionResult(
                        tool=tool_name,
                        success=True,
                        detail=f"replied to review comment: {reply.html_url}",
                    )

                try:
                    issue = repo.get_issue(number=args["pr_number"])
                    comment = issue.get_comment(comment_id)
                    reply = issue.create_comment(
                        body=f"Re: {comment.body[:100]}...\n\n{body}"
                    )
                    return ActionResult(
                        tool=tool_name,
                        success=True,
                        detail=f"replied to issue comment: {reply.html_url}",
                    )
                except Exception:
                    return ActionResult(
                        tool=tool_name,
                        success=False,
                        detail=f"could not find comment {comment_id} to reply to",
                    )

            elif tool_name == "submit_review":
                pr = repo.get_pull(args["pr_number"])
                review = pr.create_review(
                    body=args["body"],
                    event=args.get("event", "COMMENT"),
                )
                detail = getattr(review, "html_url", str(review))
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"submitted review: {detail}",
                )

            elif tool_name == "assign_reviewers":
                pr = repo.get_pull(args["pr_number"])
                pr.create_review_request(reviewers=args["reviewers"])
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"requested reviewers: {args['reviewers']}",
                )

            elif tool_name == "open_pr":
                pr = repo.create_pull(
                    title=args["title"],
                    body=args.get("body", ""),
                    head=args["head_branch"],
                    base=args["base_branch"],
                )
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"opened PR #{pr.number} {pr.html_url}",
                )

            elif tool_name == "merge_pr":
                pr = repo.get_pull(args["pr_number"])
                method = args.get("merge_method", "merge")
                res = pr.merge(merge_method=method)
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"merged: {res}",
                )

            elif tool_name == "create_branch_commit":
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
                    tool=tool_name,
                    success=True,
                    detail=f"branch {new_branch} prepared (created={created})",
                )

            elif tool_name == "get_pr_diff":
                pr = repo.get_pull(args["pr_number"])
                files = pr.get_files()
                files_list = [f.filename for f in files]
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"Fetched diff for {len(files_list)} file(s)",
                )

            elif tool_name == "update_pr_description":
                pr = repo.get_pull(args["pr_number"])
                edit_kwargs: dict[str, Any] = {}
                if "body" in args:
                    edit_kwargs["body"] = args["body"]
                if "title" in args:
                    edit_kwargs["title"] = args["title"]
                if edit_kwargs:
                    pr.edit(**edit_kwargs)
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"updated PR #{pr.number}",
                )

            elif tool_name == "create_issue":
                issue = repo.create_issue(
                    title=args["title"], body=args.get("body", "")
                )
                return ActionResult(
                    tool=tool_name,
                    success=True,
                    detail=f"created issue #{issue.number} {issue.html_url}",
                )

            else:
                return ActionResult(
                    tool=tool_name,
                    success=False,
                    detail=f"unknown tool: {tool_name}",
                )

        except Exception as exc:
            logger.exception("action execution failed")
            return ActionResult(
                tool=tool_name,
                success=False,
                detail=str(exc),
            )
