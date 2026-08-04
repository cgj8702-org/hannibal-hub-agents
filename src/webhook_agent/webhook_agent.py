"""ADK-powered webhook agent that replaces the Gemma planner.

This module defines the ADK agent with all GitHub tools as Python functions,
and provides a synchronous interface for the existing webhook pipeline.

The agent uses:
- Gemma-4-31b-it via ADK's Gemini model wrapper
- InMemoryMemoryService for in-memory conversation memory
- InMemorySessionService for per-PR conversation context
- Plain Python functions as tools (ADK auto-generates JSON schemas)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from github import Github
from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.genai import types as genai_types
from google.genai.errors import ServerError as GenAIServerError

from .bot_identity import _is_bot_event
from .memory_service import InMemoryMemoryService

logger = logging.getLogger("webhook_agent")


# ---------------------------------------------------------------------------
# Template Loading Utility
# ---------------------------------------------------------------------------


def _load_pr_template() -> str:
    """Load the PR description template from the templates directory."""
    template_path = os.path.join(
        os.path.dirname(__file__), "templates", "pr_template.md"
    )
    try:
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("PR template not found at %s", template_path)
        return ""


@dataclass
class ActionResult:
    """Result of executing a single agent tool."""

    tool: str
    success: bool
    detail: str


# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

# ---------------------------------------------------------------------------
# Input Token Safety Limits (Capped to stay under 16k/min cumulative limit)
# ---------------------------------------------------------------------------
MAX_INPUT_TOKENS = 6000  # Cap user prompt payload per turn to 6k tokens
MAX_DIFF_TOKENS = 4000  # Cap PR diff tool response to 4k tokens (~14k chars)
MAX_FILE_PATCH_CHARS = 2000  # Cap per-file diff patch in get_pr_diff


def count_tokens_exact(
    contents: str | list[Any], model_name: str = "gemma-4-31b-it"
) -> int | None:
    """Count input tokens using Google GenAI SDK's client.models.count_tokens().

    Returns exact token count from the API if credentials are configured,
    or None if unavailable/offline.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    try:
        from google import genai

        client = genai.Client(api_key=api_key) if api_key else genai.Client()
        response = client.models.count_tokens(model=model_name, contents=contents)
        return response.total_tokens
    except Exception as exc:
        logger.debug("count_tokens API call skipped/unavailable: %s", exc)
        return None


def _truncate_text_to_token_limit(
    text: str,
    max_tokens: int = MAX_INPUT_TOKENS,
    model_name: str = "gemma-4-31b-it",
    label: str = "Input",
) -> str:
    """Truncate input text to guarantee it stays strictly under max_tokens limit.

    Uses google.genai client.models.count_tokens() for exact measurement when available,
    falling back to character estimation (~3.5 chars/token).
    """
    if not text:
        return text

    # Step 1: Try exact token count via google.genai API
    exact_count = count_tokens_exact(text, model_name=model_name)

    if exact_count is not None:
        if exact_count <= max_tokens:
            return text

        # Oversized payload: iteratively truncate to fit exact token limit
        current_text = text
        current_tokens = exact_count
        while current_tokens > max_tokens and len(current_text) > 100:
            target_ratio = (max_tokens - 300) / current_tokens
            new_length = max(100, int(len(current_text) * target_ratio))
            current_text = current_text[:new_length]
            new_count = count_tokens_exact(current_text, model_name=model_name)
            if new_count is None or new_count >= current_tokens:
                current_text = current_text[: int(len(current_text) * 0.8)]
                current_tokens = int(current_tokens * 0.8)
            else:
                current_tokens = new_count

        omitted_chars = len(text) - len(current_text)
        return (
            f"{current_text}\n\n"
            f"[⚠️ {label} truncated: reduced to {current_tokens} tokens "
            f"(omitted {omitted_chars} characters) to stay under {max_tokens} token limit]"
        )

    # Step 2: Fallback character estimation if API is offline/unauthenticated
    max_chars = max_tokens * 3  # Conservative limit
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    omitted = len(text) - max_chars
    return (
        f"{truncated}\n\n"
        f"[⚠️ {label} truncated: omitted {omitted} characters (~{omitted // 4} tokens) "
        f"to stay within {max_tokens} token limit]"
    )


# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


# Retry configuration for transient server errors
_MAX_RETRIES = int(os.environ.get("GEMMA_MODEL_MAX_RETRIES", "5"))
_FALLBACK_MODEL = os.environ.get("GEMMA_MODEL_FALLBACK", "gemma-4-26b-a4b-it")


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried.

    Transient errors include server unavailability (503), rate limiting (429),
    RESOURCE_EXHAUSTED errors, and other temporary issues.
    """
    if isinstance(error, GenAIServerError):
        error_code = getattr(error, "code", None)
        return error_code in (503, 500, 429, 502, 504)
    err_str = str(error)
    if (
        "429" in err_str
        or "RESOURCE_EXHAUSTED" in err_str
        or "ResourceExhausted" in type(error).__name__
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# ADK Tool Functions — API-Aligned Primitives
# Each function becomes an ADK tool automatically. The docstring and type
# hints define the JSON schema that Gemma sees.
#
# Tools are organized by GitHub API surface:
#   Files API:  read_file, write_file
#   Issues API: get_issue, update_issue  (PRs are issues in GitHub's API)
#   Pulls API:  open_pr, merge_pr, review
# ---------------------------------------------------------------------------


def _get_gh_from_ctx(ctx: Context) -> Github:
    """Retrieve the Github client from the agent context."""
    gh = ctx.state.get("gh_client") or ctx.state.get("user:gh_client")
    if gh is None:
        raise RuntimeError("GitHub client not found in agent context")
    return gh


def _get_repo_full_name(ctx: Context) -> str:
    """Retrieve the repo full name from the agent context."""
    name = ctx.state.get("repo_full_name") or ctx.state.get("user:repo_full_name")
    if name is None:
        raise RuntimeError("repo_full_name not found in agent context")
    return name


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------


def read_file(ctx: Context, file_path: str, ref: str | None = None) -> str:
    """Read a file from the repository at a specific git ref.

    Args:
        file_path: Path to the file in the repository.
        ref: Branch name, tag, or commit SHA. Defaults to the repo default branch.

    Returns:
        The file content string, token-capped.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        kwargs: dict[str, Any] = {}
        if ref is not None:
            kwargs["ref"] = ref
        content_file = repo.get_contents(file_path, **kwargs)
        if isinstance(content_file, list):
            return f"Error: '{file_path}' is a directory, not a file."
        decoded = content_file.decoded_content.decode("utf-8", errors="replace")
        return _truncate_text_to_token_limit(
            decoded, max_tokens=MAX_DIFF_TOKENS, label="File content"
        )
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(
    ctx: Context,
    branch: str,
    file_path: str,
    content: str,
    message: str,
    base_branch: str | None = None,
) -> str:
    """Create or update a file on a branch with a commit message.

    Args:
        branch: Target branch to commit to. Created from base_branch if it does not exist.
        file_path: Path to the file in the repository.
        content: Complete file content to write.
        message: Commit message describing the change.
        base_branch: Branch to create the target from if it does not exist.

    Returns:
        A string describing the commit result.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        base = base_branch or repo.default_branch
        branch_created = False
        try:
            repo.get_branch(branch)
        except Exception:
            sb = repo.get_branch(base)
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sb.commit.sha)
            branch_created = True

        try:
            repo.create_file(file_path, message, content, branch=branch)
        except Exception:
            existing = repo.get_contents(file_path, ref=branch)
            repo.update_file(file_path, message, content, existing.sha, branch=branch)
        status = f"Committed '{file_path}' to {branch}"
        if branch_created:
            status += f" (branch created from {base})"
        return status
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# Issues API (PRs are issues in GitHub's API)
# ---------------------------------------------------------------------------


def get_issue(ctx: Context, number: int, include_diff: bool = False) -> str:
    """Get metadata for an issue or pull request.

    Args:
        number: Issue or PR number.
        include_diff: If true and the item is a PR, include file diffs and mergeability.

    Returns:
        Structured metadata. For PRs includes title, state, branches, mergeable
        status, and changed files. If include_diff is true, also includes
        token-capped file patches.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        parts: list[str] = [
            f"#{number}: {issue.title}",
            f"State: {issue.state}",
            f"Labels: {', '.join(lbl.name for lbl in issue.labels) or 'none'}",
        ]

        try:
            pr = repo.get_pull(number)
            is_pr = True
        except Exception:
            is_pr = False

        if is_pr:
            parts.append("Type: Pull Request")
            parts.append(f"Head: {pr.head.ref}")
            parts.append(f"Base: {pr.base.ref}")
            parts.append(f"Mergeable: {pr.mergeable}")
            parts.append(f"Mergeable state: {pr.mergeable_state}")
            parts.append(f"Changed files: {pr.changed_files}")
            parts.append(f"Additions: +{pr.additions}  Deletions: -{pr.deletions}")

            if include_diff:
                files = pr.get_files()
                diff_lines: list[str] = []
                for f in files:
                    patch = f.patch or "No patch available (binary/renamed/empty)."
                    if len(patch) > MAX_FILE_PATCH_CHARS:
                        omitted = len(patch) - MAX_FILE_PATCH_CHARS
                        patch = (
                            patch[:MAX_FILE_PATCH_CHARS]
                            + f"\n... [patch truncated: {omitted} chars omitted]"
                        )
                    diff_lines.append(
                        f"File: {f.filename} ({f.status})\nPatch:\n{patch}\n{'-' * 40}"
                    )
                diff_text = "\n".join(diff_lines) if diff_lines else "No files changed."
                diff_text = _truncate_text_to_token_limit(
                    diff_text, max_tokens=MAX_DIFF_TOKENS, label="PR Diff"
                )
                parts.append(f"\nDiff:\n{diff_text}")
        else:
            parts.append("Type: Issue")
            body_preview = (issue.body or "")[:500]
            if body_preview:
                parts.append(f"Body: {body_preview}")

        return "\n".join(parts)
    except Exception as e:
        return f"Error fetching issue/PR: {e}"


def update_issue(
    ctx: Context,
    number: int,
    comment: str | None = None,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    labels: list[str] | None = None,
) -> str:
    """Update an issue or pull request. Can perform multiple actions at once.

    Args:
        number: Issue or PR number.
        comment: Post a comment on the issue or PR.
        title: Update the title.
        body: Update the body or description.
        state: Set state to open or closed.
        labels: List of label names to add.

    Returns:
        A string summarizing all actions taken.
    """
    gh = _get_gh_from_ctx(ctx)
    repo_name = _get_repo_full_name(ctx)
    try:
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(number=number)
        actions: list[str] = []

        if comment:
            c = issue.create_comment(body=comment)
            actions.append(f"Commented: {c.html_url}")

        edit_kwargs: dict[str, Any] = {}
        if title is not None:
            edit_kwargs["title"] = title
        if body is not None:
            edit_kwargs["body"] = body
        if state is not None:
            edit_kwargs["state"] = state
        if edit_kwargs:
            issue.edit(**edit_kwargs)
            actions.append(f"Updated: {', '.join(edit_kwargs.keys())}")

        if labels:
            issue.add_to_labels(*labels)
            actions.append(f"Labels added: {labels}")

        return (
            f"#{number}: " + "; ".join(actions) if actions else f"#{number}: no changes"
        )
    except Exception as e:
        return f"Error updating issue/PR: {e}"


# ---------------------------------------------------------------------------
# Pulls API (PR-specific extensions)
# ---------------------------------------------------------------------------


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


def review(
    ctx: Context,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
) -> str:
    """Submit a formal review on a pull request.

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
        rv = pr.create_review(body=body, event=event)
        detail = getattr(rv, "html_url", str(rv))
        return f"Submitted review: {detail}"
    except Exception as e:
        return f"Error submitting review: {e}"


# ---------------------------------------------------------------------------
# Utility & Sub-Agent Tools
# ---------------------------------------------------------------------------


def get_current_time(ctx: Context) -> dict[str, str]:
    """Get the current UTC date and time in ISO 8601 format.

    Returns:
        A dictionary containing current_utc_time string.
    """
    return {"current_utc_time": datetime.now(timezone.utc).isoformat()}


# Sub-agent for Google Search grounding without breaking AFC for root tools
search_sub_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMMA_MODEL", "gemma-4-31b-it"),
    instruction="You are a technical search specialist. Search the web for documentation, CVEs, syntax issues, and library details.",
    tools=[google_search],
)

search_tool = AgentTool(search_sub_agent)


# ---------------------------------------------------------------------------
# System instruction for the agent
# ---------------------------------------------------------------------------

# Load the PR template once at module load time
_PR_TEMPLATE = _load_pr_template()

SYSTEM_INSTRUCTION = f"""You are a skilled autonomous GitHub Webhook Agent for the Hannibal Hub ecosystem.

Your reasoning process follows 6 steps:

1. **Understand Intent & Context**: Analyze the incoming event, user sender, PR/issue details, and conversation history.
2. **Autonomous Action Decision**: Decide if an action is required. Call tools ONLY if a user commands (/create, /review, /resolve, /analyze), asks a question, or directly mentions @hannibal-hub-agents, or for PR opened reviews. If the event is routine metadata, respond in text explaining why no tool call is needed.
3. **Validate Tool Parameters**: Verify pr_number, branch names, file_paths, and commit messages before calling tools. Use get_current_time if date/time calculations are needed.
4. **Execute Primitives**: Call read_file, write_file, get_issue, update_issue, open_pr, merge_pr, review, get_current_time, or search_agent.
5. **Format Results**: Structure reviews, PR descriptions, and responses in Markdown tables, code blocks, and clear sections.
6. **Execution Summary**: Summarize completed actions clearly.

Available tools:
  Files API:  read_file, write_file
  Issues API: get_issue, update_issue
  Pulls API:  open_pr, merge_pr, review
  Utilities:  get_current_time, search_agent (for web search & docs)

When generating PR descriptions, use this template as a guide:
{_PR_TEMPLATE}
"""

# ---------------------------------------------------------------------------
# WebhookAgent class
# ---------------------------------------------------------------------------


# Retry configuration for transient server errors
_MAX_RETRIES = int(os.environ.get("GEMMA_MODEL_MAX_RETRIES", "5"))
_FALLBACK_MODEL = os.environ.get("GEMMA_MODEL_FALLBACK", "gemma-4-26b-a4b-it")


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried.

    Transient errors include server unavailability (503), rate limiting (429),
    and other temporary issues.
    """
    if isinstance(error, GenAIServerError):
        error_code = getattr(error, "code", None)
        # Retry on 503 (UNAVAILABLE), 500 (INTERNAL_ERROR), 429 (RESOURCE_EXHAUSTED)
        return error_code in (503, 500, 429, 502, 504)
    return False


class WebhookAgent:
    """ADK-powered agent for processing GitHub webhook events.

    Wraps the ADK Agent and Runner to provide a synchronous interface
    compatible with the existing webhook pipeline.

    Supports automatic model fallback when the primary model is unavailable.
    """

    def __init__(
        self,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._app_name = "hannibal-hub-agents"

        # Session service — keeps per-PR conversation history
        self._session_service = InMemorySessionService()

        # Memory service — in-memory conversation memory
        self._memory_service = InMemoryMemoryService()

        # Track current model and fallback state
        self._current_model_name = os.environ.get("GEMMA_MODEL", "gemma-4-31b-it")
        self._fallback_triggered = False

        # Create the ADK agent with all tools
        self._agent = Agent(
            name="webhook_agent",
            model=Gemini(
                model=self._current_model_name,
            ),
            instruction=SYSTEM_INSTRUCTION,
            tools=[
                read_file,
                write_file,
                get_issue,
                update_issue,
                open_pr,
                merge_pr,
                review,
                get_current_time,
                search_tool,
            ],
        )

        # Create the runner
        self._runner = Runner(
            agent=self._agent,
            app_name=self._app_name,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _create_fallback_agent(self) -> None:
        """Switch to fallback model when primary model is unavailable."""
        if self._fallback_triggered:
            return

        logger.info(
            " Switching to fallback model: %s (primary was: %s)",
            _FALLBACK_MODEL,
            self._current_model_name,
        )
        self._fallback_triggered = True

        # Create new agent with fallback model
        self._agent = Agent(
            name="webhook_agent",
            model=Gemini(
                model=_FALLBACK_MODEL,
            ),
            instruction=SYSTEM_INSTRUCTION,
            tools=[
                read_file,
                write_file,
                get_issue,
                update_issue,
                open_pr,
                merge_pr,
                review,
                get_current_time,
                search_tool,
            ],
        )

        # Recreate runner with new agent
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
            parts.append(f"PR Head Branch: {(pr.get('head') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Base Branch: {(pr.get('base') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Additions: {pr.get('additions', 'N/A')}")
            parts.append(f"PR Deletions: {pr.get('deletions', 'N/A')}")
            parts.append(f"PR Changed Files: {pr.get('changed_files', 'N/A')}")
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
        text = _truncate_text_to_token_limit(
            text, max_tokens=MAX_INPUT_TOKENS, label="User payload"
        )
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
        canonical = event_data.get("canonical", "")

        logger.debug(
            "🔍 Checking writeback policy: canonical=%s, dry_run=%s, trace=%s",
            canonical,
            self.dry_run,
            trace_id[-4:],
        )

        if _is_bot_event(event_data):
            sender = event_data.get("sender", {})
            logger.debug(
                "🤖 Bot event detected: sender=%s, canonical=%s",
                sender.get("login", "unknown"),
                canonical,
            )
            logger.info(
                "writeback blocked: bot-originated event '%s' (trace: %s)",
                canonical,
                trace_id[-4:],
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"writeback policy: bot-originated event '{canonical}' blocked",
                )
            ]

        # Check read-only events
        read_only_events: set[str] = {
            "ping",
            "unknown",
        }
        if canonical in read_only_events:
            logger.debug(
                "📖 Read-only event detected: canonical=%s",
                canonical,
            )
            logger.info(
                "writeback policy: event '%s' is read-only (trace: %s)",
                canonical,
                trace_id[-4:],
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
            logger.debug(
                "⛔ Mutations disabled (ALLOW_AUTOMATED_MUTATIONS=%s)",
                os.environ.get("ALLOW_AUTOMATED_MUTATIONS", "0"),
            )
            logger.info(
                "mutations disabled by policy (trace: %s)",
                trace_id[-4:],
            )
            return [
                ActionResult(
                    tool="plan",
                    success=False,
                    detail="mutations are disabled by policy",
                )
            ]

        if self.dry_run:
            logger.debug("🧪 Dry-run mode enabled")
            logger.info("dry-run mode (trace: %s)", trace_id[-4:])
            return [
                ActionResult(
                    tool="plan",
                    success=True,
                    detail="dry-run: would process event through ADK agent",
                )
            ]

        logger.debug(
            "✅ All policy checks passed, building session context (trace: %s)",
            trace_id[-4:],
        )

        # Derive session and user IDs
        session_id = self._derive_session_id(event_data)
        sender = event_data.get("sender") or {}
        sender_login = sender.get("login", "")
        user_id = sender_login or "anonymous"

        logger.debug(
            "👤 Session context: session_id=%s, user_id=%s",
            session_id,
            user_id,
        )

        # Build the user message
        user_message = self._build_user_message(event_data)
        logger.debug(
            "📝 Built user message for agent (length: %d chars)",
            len(user_message.parts[0].text) if user_message.parts else 0,
        )

        # Run the agent asynchronously with retry and fallback support
        results: list[ActionResult] = []

        async def _execute_agent():
            """Execute the agent run. Returns True on success, raises on transient error."""
            logger.debug(
                "⚡ Executing ADK agent run (trace: %s, attempt=%d)",
                trace_id[-4:],
                1,  # First attempt, will be updated in retry loop
            )
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                # Handle function response events — these are tool results from ADK
                if hasattr(event, "get_function_responses"):
                    responses = event.get_function_responses()
                    if responses:
                        logger.debug(
                            "🔧 Received %d tool responses from ADK",
                            len(responses),
                        )
                        for response in responses:
                            results.append(
                                ActionResult(
                                    tool=response.name,
                                    success=True,
                                    detail=f"tool executed: {response.response}",
                                )
                            )

                # Handle text responses — log the agent's reasoning
                if (
                    event.content
                    and event.content.parts
                    and any(hasattr(p, "text") and p.text for p in event.content.parts)
                ):
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            logger.debug(
                                "💭 Agent response received (trace: %s): %s",
                                trace_id[-4:],
                                part.text[:200],
                            )
                            logger.info(
                                "🧠 Agent response: %s (trace: %s)",
                                part.text[:200],
                                trace_id[-4:],
                            )

        async def _run():
            nonlocal results
            last_error = None

            # Try with retry and optional fallback model
            for attempt in range(_MAX_RETRIES):
                try:
                    # Ensure session exists before invoking the runner.
                    # In the installed ADK version, InMemorySessionService only
                    # exposes async helpers, so we must await them here.
                    session = await self._session_service.get_session(
                        app_name=self._app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if session is None:
                        await self._session_service.create_session(
                            app_name=self._app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        # Re-fetch the session after creation
                        session = await self._session_service.get_session(
                            app_name=self._app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Created new ADK session %s for user %s",
                            session_id,
                            user_id,
                        )

                    # Set user_state values - they get merged into session.state by InMemorySessionService
                    # This is needed because session copies are returned and our direct mutations wouldn't persist
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["gh_client"] = gh_client
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["repo_full_name"] = repo_full_name
                    self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})["sender"] = user_id

                    await _execute_agent()
                    return  # Success - exit the retry loop

                except GenAIServerError as e:
                    last_error = e
                    if _is_transient_error(e) and attempt < _MAX_RETRIES - 1:
                        logger.debug(
                            "Transient API error on attempt %d/%d (trace: %s): code=%s, error=%s",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            getattr(e, "code", "unknown"),
                            e,
                        )
                        logger.warning(
                            "Transient error on attempt %d/%d (trace: %s): %s",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            e,
                        )
                        # Try fallback model after first transient error
                        if attempt == 0 and not self._fallback_triggered:
                            logger.debug("Triggering fallback model switch (attempt 1)")
                            self._create_fallback_agent()
                        continue
                    logger.debug(
                        "Non-transient error or exhausted retries: raising exception (trace: %s)",
                        trace_id[-4:],
                    )
                    raise  # Non-transient error or exhausted retries

                except Exception as e:
                    # Non-server errors are not retried
                    logger.debug(
                        "Non-server error during agent run (trace: %s): type=%s",
                        trace_id[-4:],
                        type(e).__name__,
                    )
                    logger.exception(
                        "ADK agent run failed (trace: %s): %s",
                        trace_id[-4:],
                        e,
                    )
                    results.append(
                        ActionResult(
                            tool="plan",
                            success=False,
                            detail=f"ADK agent error: {e}",
                        )
                    )
                    return

            # If we exhausted retries, add error result
            if last_error and _is_transient_error(last_error):
                logger.debug(
                    "All retry attempts exhausted (trace: %s): retrying model was unavailable",
                    trace_id[-4:],
                )
                logger.error(
                    "Model unavailable after %d retries (trace: %s)",
                    _MAX_RETRIES,
                    trace_id[-4:],
                )
                results.append(
                    ActionResult(
                        tool="plan",
                        success=False,
                        detail=f"Model unavailable after {_MAX_RETRIES} retries: {last_error}",
                    )
                )

        asyncio.run(_run())

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id[-4:],
            )

        return results
