"""WebhookAgent ADK agent definition and execution orchestration.

Extracted from webhook_agent.py as part of Phase 4 modularization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from github import Github
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Workflow
from google.genai import types as genai_types

from webhook_agent.bot_identity import _is_bot_event
from webhook_agent.callbacks import (
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from webhook_agent.core.github_tools import (
    get_commit_diff,
    get_current_time,
    get_issue,
    read_file,
    review,
)
from webhook_agent.core.loop_helpers import (
    get_shared_genai_client,
    run_in_bg_loop,
)
from webhook_agent.logic.model_chain import (
    _DEPLETED_MODEL_REGISTRY,
    _is_transient_error,
    _select_model_for_event,
    get_active_model,
    is_gemini_3_plus,
)
from webhook_agent.logic.model_factory import get_adk_model
from webhook_agent.logic.plugins import (
    ToolOutputPruningPlugin,
    WebhookHistoryPruningPlugin,
)
from webhook_agent.logic.rate_limiter import (
    _resolve_tier,
    extract_rate_limit_details,
    get_active_api_key,
    rpm_waiter,
)
from webhook_agent.logic.writeback_policy import (
    _is_formal_review_eligible,
    evaluate_writeback_policy,
)
from webhook_agent.memory_service import InMemoryMemoryService
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.sanitizer_plugin import PromptSanitizerPlugin
from webhook_agent.tools import resolve_conflicts as resolve_conflicts_module
from webhook_agent.tools.ast_tools import verify_python_ast_tool
from webhook_agent.tools.codebase_search import search_codebase_tool
from webhook_agent.tools.diff_tools import (
    get_pr_diff_file_map_tool,
    verify_line_reference_tool,
)
from webhook_agent.tools.search_tool import google_search_grounding_tool
from webhook_agent.tools.symbol_tools import check_symbol_impact_tool
from webhook_agent.tools.test_impact_tools import check_test_coverage_tool
from webhook_agent.webhook_types import ActionResult

logger = logging.getLogger("webhook_agent.core.agent_definition")

# Retry configuration for transient server errors
_MAX_RETRIES = int(
    os.environ.get("PRIMARY_MODEL_MAX_RETRIES") or os.environ.get("GEMMA_MODEL_MAX_RETRIES") or "5"
)
_FALLBACK_MODEL = (
    os.environ.get("PRIMARY_MODEL_FALLBACK")
    or os.environ.get("GEMMA_MODEL_FALLBACK")
    or "gemini-3.5-flash-lite"
)

# Bot identity — used for writeback policy
BOT_LOGIN = "hannibal-hub-agents[bot]"

# Input Token Safety Limits (Capped to stay under token budget)
MAX_INPUT_TOKENS = 3500  # Default 3.5k tokens cap for Free Tier / Gemma models


def get_max_input_tokens() -> int:
    """Return max token input budget based on active tier capability.

    Free Tier / Gemma models: 3,500 tokens.
    Paid Tier Gemini Flash models: 35,000 tokens (10x context window).
    """
    tier = _resolve_tier()
    if tier == "paid":
        return 35000
    return MAX_INPUT_TOKENS


def count_tokens_exact(contents: str | list[Any], model_name: str | None = None) -> int | None:
    """Count input tokens using Google GenAI SDK's client.models.count_tokens()."""
    target_model = model_name or get_active_model()
    try:
        client = get_shared_genai_client()
        if client is None:
            return None
        res = client.models.count_tokens(
            model=target_model,
            contents=contents if isinstance(contents, list) else [contents],
        )
        return getattr(res, "total_tokens", None)
    except Exception as exc:
        logger.debug("count_tokens API call skipped/unavailable: %s", exc)
        return None


def _truncate_input_for_tier(
    text: str,
    model: str = "default",
    tier: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Preserve full text payload without truncation."""
    return text


def _truncate_text_to_token_limit(
    text: str,
    max_tokens: int | None = None,
    model_name: str | None = None,
    label: str = "Input",
) -> str:
    """Preserve full text payload without truncation."""
    return text


def calculate_verdict(
    scores: dict[str, int] | None = None,
    confidence: int = 5,
    has_critical: bool = False,
) -> str:
    """Calculates PR review verdict cleanly.

    Rules:
    - If has_critical: REQUEST_CHANGES
    - Otherwise: APPROVE
    """
    if has_critical:
        return "REQUEST_CHANGES"
    return "APPROVE"


SYSTEM_INSTRUCTION = """You are a Senior Autonomous Engineer and Code Auditor for the Hannibal Hub ecosystem.

Your core mission is to protect repository hygiene, audit code changes with clinical precision, and generate pristine, actionable technical feedback. Zero sycophancy or generic cheerleading is permitted.

### Reasoning & Grounding Principles

1. **Understand Context**: Analyze user requests, pull request diffs, pre-compiled AST dossier, and codebase structure.
2. **Grounding & Codebase Investigation Pre-Check**:
   - Before claiming that code, environment variable defaults, teardown blocks, or unit tests are missing in a PR review:
   - You MUST call `search_codebase` and `read_file` to search and inspect target files first.
   - Internalize reasoning via your native thinking capabilities to formulate hypotheses and test them against diffs and codebase context.
3. **STRICT PROHIBITION ON ASKING QUESTIONS IN OUTPUT**:
   - DO NOT output open questions, speculative queries, or rhetorical prompts (e.g. "Can we verify...", "Is there a reason...", "Should we check...") to the PR author in final review output.
   - If you have questions about existing code, conventions, default environment variables, or behavior, use `search_codebase` and `read_file` to find the answers yourself during execution.
   - All final review action items MUST be concrete, verified technical assertions with exact file and line citations.
4. **Code Snippet & Backtick Formatting**:
   - All code snippets in `suggested_fix` or inline recommendations MUST be properly wrapped in backticks (`code`) for single-line expressions or valid markdown code blocks (```python ... ```) for multi-line code.
5. **Exact Tool Names**: Call tools using their exact function names (`search_codebase`, `read_file`, `review`, `verify_python_ast`, `check_symbol_impact`, etc.) without any prefix.
6. **Deterministic AST & Structural Integrity Verification**:
   - The deterministic pre-audit compiler already executes syntax parsing and AST node validation on modified Python files, embedding findings directly into your prompt.
   - You do NOT need to call `verify_python_ast` if findings are already provided in the pre-audit compiler dossier. The `review()` tool automatically honors the pre-compiled dossier. Call `verify_python_ast` only if you need to inspect an unlisted Python file.
7. **Cross-File Contract & Symbol Impact Verification**:
   - The deterministic symbol impact analyzer already maps modified signatures against the repository call graph and embeds breaking alterations in your prompt. Call `check_symbol_impact` only if an unlisted symbol requires additional checking.
8. **Fast-Pass Review Velocity (1 to 2 Turns Target)**:
   - Maximize audit velocity and conserve API rate limits. Evaluate the pre-compiled dossier, diff, and contract impact, then call `review()` directly. Avoid chatty or exploratory tool loops unless inspecting an external file strictly required for grounding.
9. **Test Coverage & Regression Invariants**:
   - The deterministic test impact engine has scanned matching test suites under `tests/`. Verify whether modified symbols have unit test coverage. If coverage is verified, cite the test cases in `verified_invariants`. If coverage is missing, provide a concrete unit test recommendation under `minor_suggestions` using the recommended test stub.

---

## Code Review Protocol (MANDATORY)

You are a SENIOR ENGINEER performing code reviews, not a cheerleader. Your job is to catch problems, protect code quality, and provide honest, actionable feedback. Agreeing with everything is a failure mode.

### Review Procedure

When reviewing a PR, you MUST:
1. **For Initial PR Creation (`pull_request.opened` or `/review`)**:
   - Evaluate every changed file systematically across **4 Mandatory Audit Dimensions**:
     1) **Logic & Boundaries**: Off-by-one errors, null/None dereferences, unhandled exceptions, resource leaks.
     2) **Concurrency & Memory**: Async race conditions, shared state mutation without locks, memory growth.
     3) **Security & Secrets**: Hardcoded secrets, input sanitization, authentication/authorization boundaries.
     4) **Contract Integrity**: Breaking signature changes, missing invocation site updates across the codebase.
   - Output your review response as a VALID JSON object matching the `CodeReviewResponse` schema with fields: `executive_summary`, `critical_issues`, `minor_suggestions`, `risks_and_edge_cases`, `verified_invariants`, `context_gaps`. When calling `review()`, pass this JSON string as the `body` parameter. Do NOT pass raw Markdown into `review()`; the system deterministically renders clean GitHub Markdown from your validated JSON.
   - For an `APPROVE` verdict, you MUST include at least one concrete invariant, edge case, or contract in `verified_invariants` with exact `path`, positive integer `line`, and clinical `evidence`.
   - For each actionable bug or improvement in `critical_issues` or `minor_suggestions`, specify the exact `path`, `line`, and clinical replacement code in `suggested_fix`. This enables native GitHub Suggested Change inline review comments (` ```suggestion `).

2. **For PR Updates & Re-reviews (`pull_request.synchronize`)**:
   - Review the pre-fetched incremental commit diff (`commit_diff`) and compare it against `previous_bot_reviews`.
   - Output your review response as a VALID JSON object matching the `SyncReviewResponse` schema with fields: `summary`, `resolutions`, `critical_issues`, `minor_suggestions`, `verified_invariants`. When calling `review()`, pass this JSON string as the `body` parameter.
   - For an `APPROVE` verdict, you MUST include at least one concrete invariant, edge case, or contract in `verified_invariants` with exact `path`, positive integer `line`, and clinical `evidence`.
   - For new findings in `critical_issues` or `minor_suggestions`, provide `path`, `line`, and `suggested_fix`.
   - Track items in `resolutions` across all three feedback dimensions raised in `previous_bot_reviews`:
     1) **Critical Issues** (`category: "CRITICAL"`): Verify whether blocking issues were resolved.
     2) **Suggestions & Maintainability** (`category: "SUGGESTION"`): Verify whether suggested improvements were adopted.
     3) **Potential Risks & Edge Cases** (`category: "RISK"`): Verify whether potential edge cases, concurrency risks, or limits were mitigated.
   - If there were no prior review items, suggestions, or risks, leave `resolutions` as an empty list `[]`. Never invent or backfill resolved items from the new commit's changes that were not in prior reviews.
   - For items that were in `previous_bot_reviews`, mark every previously identified finding as `RESOLVED` or `UNRESOLVED` with line citations and evidence, setting `category` accordingly.
   - Distinguish PR-authored commits from base branch merges (`Merge branch 'main' ...`). Commits originating from merging or updating from the base branch are part of the target branch and must NOT be attributed to the PR author or flagged as scope creep.

### Verdict Rules & Strict Review Tool Rejection (Non-Negotiable)

These rules override your judgment. Apply them mechanically based on your findings:
- ANY critical issue -> event MUST be REQUEST_CHANGES
- 0 critical issues and 0 unresolved items -> event MAY be APPROVE
- **STRICT TOOL VALIDATION**: The `review()` tool will REJECT and error out your submission if:
  1) Any critical issue or minor suggestion lacks an exact file `path` from the diff (generic paths like `"codebase"` or `"unknown"` will be rejected).
  2) Any finding lacks a positive integer `line` number (> 0).
  3) Any finding lacks concrete code in `suggested_fix` or uses generic boilerplate (e.g. "Address requested changes before merge").
  4) You pass `REQUEST_CHANGES` without at least one actionable critical issue (or an UNRESOLVED item in sync reviews).
  5) The PR modifies Python files (`.py`), but you neither had pre-compiled AST verification nor executed `verify_python_ast` before calling `review()`.
  6) You pass `APPROVE`, but `verified_invariants` is missing or empty. An `APPROVE` verdict strictly requires at least one concrete invariant/boundary condition with exact `path`, positive integer `line`, and concrete `evidence`. If no invariant is verified, change verdict to `COMMENT` or `REQUEST_CHANGES`.
  If `review()` returns an error, examine the rejection details, locate the exact file and line from the diff, provide real replacement code, and call `review()` again.

### Critical Thinking & Anti-Sycophancy Requirements

- **NO SYCOPHANCY / NO CHEERLEADING**: Do NOT use performative praise or generic cheerleading like "Splendid refactoring!", "Exemplary implementation!", or "Rock-solid PR!". State objective technical facts only.
- **HIGH-SIGNAL RISK & EDGE-CASE ANALYSIS**: Highlight genuine potential failure modes, unhandled edge cases, rate limits, timeout risks, or concurrency boundaries when present.
- Every review should aim to include actionable, specific suggestions with file:line citations when improvements are possible.
- Never say code is "verified" without citing specific evidence from the diff for each claim.
- Do not summarize what the code does back to the author — focus on what could go WRONG.
- If the PR is large (>500 lines changed), recommend splitting it and note this in your review.

### Review Voice & Comment Style (Clinical & Assertive)

- Zero emojis inside code comments, suggestions, or inline reviews.
- No severity labels or bold prefixes (e.g. `**Critical:**`, `[HIGH]`, `🔴`) inside comment bodies.
- No markdown headers or bullet lists inside inline comments.
- Never write vague quality prose like "Consider refactoring to improve readability and maintainability" — cite observable facts at the line.
- Avoid greetings, sign-offs, or thanking the author.
- Do NOT output questions or rhetorical queries in comments. State verified defects or remedies directly.

### Diff Grounding & The "Observable Defect" Filter
- A finding must point to something **DIRECTLY OBSERVABLE** in the diff at the line you anchor it to.
- Do NOT report that something is absent (e.g. "import is missing", "function is not defined") unless you are reviewing a newly added file in full. In partial diffs, definitions normally exist outside the hunk.
- Do NOT speculate on issues that require tracing across unshown files, guessing external inputs, or executing code. If a finding cannot be verified from the visible diff lines alone, drop it.
- **Diff Scan Protocol**: File by file, scan the diff and formulate your thoughts using native reasoning and `search_codebase` if external context is needed. Then output your final findings as EXACTLY ONE JSON object conforming to `CodeReviewResponse` or `SyncReviewResponse`.

### Dependabot / Dependency PR Protocol (MANDATORY)

When reviewing Dependabot PRs (`sender: dependabot[bot]` or branch starting with `dependabot/`):
- Focus on **dependency security, version scope, and lockfile integrity**.
- Do NOT perform a human architectural code review - evaluate version bumps and lockfile changes.
- Check if `pyproject.toml` or `package.json` updates match `uv.lock` or `package-lock.json`.
- If lockfile changes modify unrelated packages or introduce breaking dependency corruption unexpectedly, you MUST select `REQUEST_CHANGES`, set `verdict: "REQUEST_CHANGES"`, and add a blocking entry directly under `critical_issues`. NEVER place breaking lockfile corruption solely in `risks_and_edge_cases`.
- **Transitive Dependencies**: If a package bump in `uv.lock` triggers accompanying version bumps in direct or transitive dependencies (e.g. `librt` or `ast-serialize` accompanying `mypy`), and these packages are in the dependency graph of the primary package, they are REQUIRED transitive dependencies. Do NOT flag valid transitive dependency updates as unauthorized scope creep or critical issues.
- **Base branch syncs and merge commits**: Commits merged from the base branch (`main`) into a PR branch (e.g., via GitHub's 'Update branch' button or `git merge main`) belong to the base branch. Do NOT attribute base branch changes or merge commits to the PR author or flag them as scope violations.

"""

AUDITOR_CONTEXT_INSTRUCTION = """Always use the original user message, PR metadata, and full PR diff as the source of truth for this audit.

"""


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

        # Track current model chain (TPM Descending)
        from webhook_agent import webhook_agent as wa_mod
        from webhook_agent.logic.model_chain import get_model_chain as default_get_chain

        get_chain_fn = getattr(wa_mod, "get_model_chain", default_get_chain) or default_get_chain
        self._model_chain = get_chain_fn()
        self._chain_index = 0
        self._current_model_name = self._model_chain[self._chain_index]
        self._attempted_model_names = {self._normalize_model_name(self._current_model_name)}
        self._fallback_triggered = False

        # Ensure API key is resolved and propagated to env vars before model init
        get_active_api_key()

        # Model instance for pipeline sub-agents
        model_instance = get_adk_model(
            model_name=self._current_model_name,
            api_key=get_active_api_key(),
        )
        PromptSanitizerPlugin()

        # Streamlined workflow: START -> code_auditor
        # Single-pass high-velocity auditor eliminating redundant sub-agent hops.
        auditor_thinking_budget = int(os.environ.get("AUDITOR_THINKING_BUDGET", "1024"))

        self._code_auditor = LlmAgent(
            name="code_auditor",
            model=model_instance,
            include_contents="default",
            description="Conducts AST diff-grounded risk audit using Gemini Thinking Mode.",
            instruction=AUDITOR_CONTEXT_INSTRUCTION + SYSTEM_INSTRUCTION,
            output_key="code_review_analysis",
            planner=BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=False,
                    thinking_budget=auditor_thinking_budget,
                )
            ),
            before_agent_callback=before_agent_callback,
            before_model_callback=before_model_callback,
            after_model_callback=after_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            on_tool_error_callback=on_tool_error_callback,
            tools=[
                read_file,
                get_issue,
                get_commit_diff,
                review,
                get_current_time,
                get_pr_diff_file_map_tool,
                verify_line_reference_tool,
                verify_python_ast_tool,
                check_symbol_impact_tool,
                check_test_coverage_tool,
                google_search_grounding_tool,
                search_codebase_tool,
            ],
        )

        self._agent = Workflow(
            name="webhook_agent",
            edges=[
                (START, self._code_auditor),
            ],
        )

        self._history_pruning_plugin = WebhookHistoryPruningPlugin(max_events=12)
        self._tool_pruning_plugin = ToolOutputPruningPlugin()

        # Context Caching: strictly only for Gemini 3+ models (min 4096 tokens).
        # Gemma models (e.g. gemma-4-31b-it) do NOT support context caching.
        context_cache_config = (
            ContextCacheConfig(
                min_tokens=4096,
                ttl_seconds=1800,
                cache_intervals=10,
            )
            if is_gemini_3_plus(self._current_model_name)
            else None
        )

        self._app = App(
            name=self._app_name,
            root_agent=self._agent,
            context_cache_config=context_cache_config,
            plugins=[
                self._history_pruning_plugin,
                self._tool_pruning_plugin,
            ],
        )

        # Create the runner
        self._runner = Runner(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        return model_name.replace("models/", "").strip().lower()

    def _advance_model_chain(self, error: Exception | None = None) -> str | None:
        """Cascade once to the next untried model, or return None when exhausted."""
        from webhook_agent import webhook_agent as wa_mod

        failed_model = self._normalize_model_name(self._current_model_name)
        self._attempted_model_names.add(failed_model)
        depleted_registry = getattr(wa_mod, "_DEPLETED_MODEL_REGISTRY", _DEPLETED_MODEL_REGISTRY)
        depleted_registry.mark_depleted(self._current_model_name, error=error)

        from webhook_agent.logic.model_chain import get_model_chain as default_get_chain

        get_chain_fn = getattr(wa_mod, "get_model_chain", default_get_chain) or default_get_chain
        full_chain = get_chain_fn()
        available = [
            model
            for model in full_chain
            if self._normalize_model_name(model) not in self._attempted_model_names
        ]
        self._model_chain = available
        self._chain_index = 0

        if not self._model_chain:
            logger.error("All configured models have been attempted for this agent run")
            return None

        next_model = self._model_chain[0]
        logger.warning(
            "⚠️ Cascading model chain from %s -> %s",
            self._current_model_name,
            next_model,
        )
        self._current_model_name = next_model
        self._attempted_model_names.add(self._normalize_model_name(next_model))
        new_model_instance = get_adk_model(
            model_name=next_model,
            api_key=get_active_api_key(),
        )
        self._code_auditor.model = new_model_instance

        # Dynamically toggle context caching based on model family:
        # Strictly enable only for Gemini 3+ models; disable for Gemma / legacy models.
        if is_gemini_3_plus(next_model):
            self._app.context_cache_config = ContextCacheConfig(
                min_tokens=4096,
                ttl_seconds=1800,
                cache_intervals=10,
            )
        else:
            self._app.context_cache_config = None

        runner_cls = getattr(wa_mod, "Runner", Runner)
        self._runner = runner_cls(
            app=self._app,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )
        return next_model

    def _create_fallback_agent(self, error: Exception | None = None) -> None:
        """Switch to fallback model when primary model is unavailable."""
        self._advance_model_chain(error=error)

        # Recreate runner with new agent
        self._runner = Runner(
            app=self._app,
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
            comment_body = comment.get("body") or ""
            is_pr = bool(issue.get("pull_request"))
            pr_num = issue.get("number", "unknown")
            parts.append(f"Issue/PR Number: {pr_num}")
            parts.append(f"Thread Type: {'Pull Request' if is_pr else 'Issue'}")
            parts.append(f"Comment: {comment_body}")
            if is_pr:
                parts.append(
                    f"Note: This comment is on Pull Request #{pr_num}. "
                    f"To perform requested actions like code reviews (/review), descriptions (/create), "
                    f"or conflict resolution (/resolve), first call get_issue({pr_num}, include_diff=True) "
                    f"to inspect the PR metadata and code changes."
                )
        elif canonical.startswith("pull_request."):
            pr = raw.get("pull_request", {})
            pr_num = pr.get("number", "unknown")
            parts.append(f"PR Number: {pr_num}")
            parts.append(f"PR Title: {pr.get('title', 'N/A')}")
            parts.append(f"PR Body: {pr.get('body') or ''}")
            parts.append(f"PR Head Branch: {(pr.get('head') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Base Branch: {(pr.get('base') or {}).get('ref', 'N/A')}")
            parts.append(f"PR Additions: {pr.get('additions', 'N/A')}")
            parts.append(f"PR Deletions: {pr.get('deletions', 'N/A')}")
            parts.append(f"PR Changed Files: {pr.get('changed_files', 'N/A')}")

            if canonical == "pull_request.synchronize" or raw.get("action") == "synchronize":
                before_sha = raw.get("before", "")
                head_sha = (pr.get("head") or {}).get("sha", "")
                parts.append(
                    f"\n[NEW COMMIT PUSHED - Event: pull_request.synchronize]\n"
                    f"Before SHA: {before_sha}\n"
                    f"Head SHA: {head_sha}\n"
                    f"MANDATORY INSTRUCTION: A new commit was pushed to PR #{pr_num}. "
                    f"Review the pre-fetched incremental commit diff and full PR diff below to evaluate "
                    f"the changes in turn 1 and submit an updated formal review (APPROVE or REQUEST_CHANGES)."
                )
        elif canonical.startswith("pull_request_review_comment."):
            comment = raw.get("comment", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review Comment: {comment.get('body') or ''}")
        elif canonical.startswith("pull_request_review."):
            review = raw.get("review", {})
            pr = raw.get("pull_request", {})
            parts.append(f"PR Number: {pr.get('number', 'unknown')}")
            parts.append(f"Review: {review.get('body') or ''}")

        # Include pre-fetched commit diff (incremental changes) if available
        if "commit_diff" in raw:
            parts.append(f"\nNew Commit Diff (Incremental Changes):\n{raw['commit_diff']}")

        # Include pre-fetched PR diff (full accumulated state) if available.
        # The deterministic file gate is derived from the complete inventory.
        if "pr_diff" in raw:
            changed_files = raw.get("changed_files") or []
            inventory = ", ".join(changed_files) or "unavailable"
            parts.append(f"Changed file inventory: {inventory}")
            parts.append(f"\nFull PR Diff (Accumulated State):\n{raw['pr_diff']}")

            # Ground transitive dependency updates when uv.lock is modified
            if any(f.endswith("uv.lock") for f in changed_files):
                try:
                    from ..logic.dependency_tree import build_dependency_grounding_context
                    from ..logic.lockfile_validator import parse_bumped_packages_from_diff

                    bumps = parse_bumped_packages_from_diff(raw["pr_diff"])
                    bumped_names = [b["name"] for b in bumps]
                    grounding_block = build_dependency_grounding_context(
                        bumped_packages=bumped_names,
                        lockfile_text=raw["pr_diff"],
                    )
                    if grounding_block:
                        parts.append(f"\n{grounding_block}")
                except Exception as grounding_err:
                    logger.debug("Could not build dependency grounding block: %s", grounding_err)

            # Option 3: Deterministic Pre-Review Compiler Dossier
            # Run AST integrity analysis on modified Python files
            py_files = [f for f in changed_files if f.endswith(".py")]
            if py_files:
                try:
                    from webhook_agent.tools.ast_tools import verify_python_ast

                    dossier_items: list[str] = []
                    for py_file in py_files[:5]:
                        ast_res = verify_python_ast(file_path=py_file)
                        if not ast_res.startswith("Error: No code snippet provided and file"):
                            dossier_items.append(ast_res)
                    if dossier_items:
                        dossier_text = "\n\n".join(dossier_items)
                        parts.append(
                            f"\n### 🔍 Deterministic Pre-Audit Compiler Findings\n"
                            f"The deterministic AST integrity engine pre-audited modified Python files:\n\n"
                            f"{dossier_text}\n\n"
                            f"Use these findings to focus your audit on structural defects, risk areas, and verified AST nodes."
                        )
                        event_data["deterministic_precompiled_ast"] = True

                    # Cross-File Symbol Dependency & Breaking Signature Graph
                    from webhook_agent.logic.symbol_graph import SymbolImpactAnalyzer

                    sym_analyzer = SymbolImpactAnalyzer()
                    impact_reports = sym_analyzer.analyze_modified_files(changed_files)
                    impact_blocks = [
                        r.to_markdown()
                        for r in impact_reports
                        if r.broken_call_sites or (r.sig_diff and r.sig_diff.is_breaking)
                    ]
                    if impact_blocks:
                        impact_text = "\n\n".join(impact_blocks)
                        parts.append(
                            f"\n### 🕸️ Cross-File Contract & Symbol Impact Analysis\n"
                            f"The static symbol dependency engine detected contract alterations and cross-file callers:\n\n"
                            f"{impact_text}\n\n"
                            f"Verify whether external callers are broken and enforce Dimension 4 (Contract Integrity)."
                        )

                    # Test Impact & Coverage Verification
                    from webhook_agent.logic.test_impact import TestImpactAnalyzer

                    test_analyzer = TestImpactAnalyzer()
                    test_report = test_analyzer.analyze(changed_files)
                    if test_report.coverages:
                        test_md = test_report.to_markdown()
                        parts.append(f"\n{test_md}")
                except Exception as ast_err:
                    logger.debug(
                        "Could not build deterministic pre-audit compiler dossier: %s", ast_err
                    )

        # Include pre-fetched inline comment code context if available
        if "inline_code_context" in raw:
            parts.append(f"\nPre-Fetched Inline Code Context:\n{raw['inline_code_context']}")

        # Include pre-executed conflict resolution result if available
        if "conflict_resolution_result" in raw:
            res = raw["conflict_resolution_result"]
            parts.append(
                f"\nPre-Executed Conflict Resolution Result:\n"
                f"Status: {'Success' if res.get('success') else 'Failed'}\n"
                f"Detail: {res.get('detail') or res.get('error') or 'N/A'}"
            )

        # Include pre-fetched commit history summary if available
        if "commit_history_summary" in raw:
            parts.append(f"\nPre-Fetched Commit History Summary:\n{raw['commit_history_summary']}")

        # Include pre-fetched previous bot reviews if available
        if "previous_bot_reviews" in raw:
            parts.append(f"\nPre-Fetched Previous Bot Reviews:\n{raw['previous_bot_reviews']}")
            has_prior_items = raw.get("prior_reviews_had_findings", False) or raw.get(
                "prior_reviews_had_request_changes", False
            )
            if not has_prior_items:
                parts.append(
                    "\nNOTE ON RESOLUTION TRACKER: No prior review identified actionable critical issues, "
                    "suggestions, or risks on this PR. You MUST leave 'resolutions' as an empty list ([]) "
                    "in SyncReviewResponse. Do NOT invent or backfill resolved items."
                )
            else:
                parts.append(
                    "\nNOTE ON RESOLUTION TRACKER: Evaluate whether the new commits address or mitigate "
                    "the previously identified Critical Issues, Suggestions & Maintainability items, or "
                    "Potential Risks & Edge Cases. Mark each item in 'resolutions' as RESOLVED or "
                    "UNRESOLVED with diff evidence, setting 'category' to 'CRITICAL', 'SUGGESTION', or 'RISK'."
                )

        if canonical in ("pull_request.opened", "pull_request.synchronize") or (
            canonical.startswith("issue_comment.") and "/review" in comment_body
        ):
            parts.append(
                "\n### 🚀 ACTION DIRECTIVE: FAST-PASS FORMAL AUDIT\n"
                "Evaluate the pre-fetched PR diff, AST verification findings, symbol impact analysis, "
                "and test coverage findings above.\n"
                "In Turn 1, call the `review()` tool directly with your completed CodeReviewResponse (or SyncReviewResponse) "
                "JSON payload (event='APPROVE' or 'REQUEST_CHANGES').\n"
                "Do NOT perform exploratory search or file inspection unless strictly required for a critical invariant. "
                "Submit your formal review immediately."
            )

        text = "\n".join(parts)
        text = _truncate_text_to_token_limit(
            text, max_tokens=get_max_input_tokens(), label="User payload"
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
        canonical = event_data.get("canonical", "")

        # Check writeback policy (bot-authored, read-only, closed PR, mutations disabled, dry-run)
        policy_actions = evaluate_writeback_policy(
            event_data=event_data,
            dry_run=self.dry_run,
            trace_id=trace_id,
            is_bot_event_fn=_is_bot_event,
        )
        if policy_actions is not None:
            return policy_actions

        logger.debug(
            "✅ All policy checks passed, building session context (trace: %s)",
            trace_id[-4:],
        )

        # Programmatic Command Router: Intercept /resolve slash command for instant Git Worktree conflict resolution
        raw = event_data.get("raw_payload", {})
        comment_body = ""
        if isinstance(raw, dict) and isinstance(raw.get("comment"), dict):
            comment_body = (raw["comment"].get("body") or "").strip()

        if "/resolve" in comment_body.lower():
            if isinstance(raw, dict) and "conflict_resolution_result" in raw:
                res = raw["conflict_resolution_result"]
                return [
                    ActionResult(
                        tool="resolve_merge_conflicts",
                        success=res.get("success", False),
                        detail=res.get("detail", ""),
                    )
                ]

            pr_number = None
            if isinstance(raw, dict):
                pr_number = (raw.get("pull_request") or {}).get("number") or (
                    raw.get("issue") or {}
                ).get("number")

            if pr_number:
                selected_model = _select_model_for_event(event_data)
                logger.info(
                    "⚡ Programmatic Command Router: Intercepted /resolve for PR #%d (trace: %s, model: %s)",
                    pr_number,
                    trace_id[-4:],
                    selected_model,
                )
                try:
                    repo = gh_client.get_repo(repo_full_name)
                    pr = repo.get_pull(pr_number)
                    genai_client = get_shared_genai_client()
                    token = getattr(getattr(gh_client, "_auth", None), "token", None)
                    # Dispatch via webhook_agent to allow monkeypatching/mocking in tests
                    from webhook_agent import webhook_agent as wa_mod

                    resolve_fn = getattr(
                        wa_mod,
                        "resolve_merge_conflicts",
                        resolve_conflicts_module.resolve_merge_conflicts,
                    )
                    res = resolve_fn(
                        pr_number=pr_number,
                        head_branch=pr.head.ref,
                        base_branch=pr.base.ref,
                        genai_client=genai_client,
                        model_name=selected_model,
                        token=token,
                    )
                    status_detail = res.get("detail", "")
                    if res.get("success"):
                        comment_text = (
                            f"I have surgically resolved the merge conflicts for PR #{pr_number} "
                            f"against `{pr.base.ref}` using an isolated Git Worktree and pushed the updated branch.\n\n"
                            f"**Detail:** {status_detail}"
                        )
                    else:
                        comment_text = (
                            f"Unable to automatically resolve merge conflicts for PR #{pr_number}.\n\n"
                            f"**Detail:** {status_detail}"
                        )
                    pr.create_issue_comment(comment_text)
                    return [
                        ActionResult(
                            tool="resolve_merge_conflicts",
                            success=res.get("success", False),
                            detail=status_detail,
                        )
                    ]
                except Exception as exc:
                    logger.exception(
                        "Programmatic /resolve execution failed for PR #%d: %s",
                        pr_number,
                        exc,
                    )
                    return [
                        ActionResult(
                            tool="resolve_merge_conflicts",
                            success=False,
                            detail=f"Programmatic /resolve failed: {exc}",
                        )
                    ]

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
            len(getattr(user_message.parts[0], "text", "") or "") if user_message.parts else 0,
        )

        # Select model tier dynamically for this event
        selected_model = _select_model_for_event(event_data)
        if self._current_model_name != selected_model:
            logger.info(
                "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
                selected_model,
                canonical,
                trace_id[-4:],
            )
            self._current_model_name = selected_model
            new_model_instance = get_adk_model(
                model_name=selected_model,
                api_key=get_active_api_key(),
            )
            self._code_auditor.model = new_model_instance

        # Run the agent asynchronously with retry and fallback support
        results: list[ActionResult] = []
        emitted_texts: list[str] = []
        final_session = None

        async def _execute_agent() -> None:
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                # Handle token recording if usage metadata is available
                if hasattr(event, "usage_metadata") and event.usage_metadata:
                    total_tok = getattr(event.usage_metadata, "total_token_count", 0) or getattr(
                        event.usage_metadata, "total_tokens", 0
                    )
                    if total_tok > 0:
                        await rpm_waiter.record_actual_tokens(
                            model=self._current_model_name,
                            actual_tokens=total_tok,
                        )

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
                                    tool=str(response.name or ""),
                                    success=True,
                                    detail=f"tool executed: {response.response}",
                                )
                            )

                # Handle text responses — log the agent's reasoning (filtering out thought tokens)
                if (
                    event.content
                    and event.content.parts
                    and any(
                        hasattr(p, "text") and p.text and not getattr(p, "thought", False)
                        for p in event.content.parts
                    )
                ):
                    for part in event.content.parts:
                        if getattr(part, "thought", False):
                            continue
                        if hasattr(part, "text") and part.text:
                            emitted_texts.append(part.text)
                            logger.debug(
                                "💭 Agent response received (trace: %s): %s",
                                trace_id[-4:],
                                part.text,
                            )
                            logger.info(
                                "🧠 Agent response: %s (trace: %s)",
                                part.text,
                                trace_id[-4:],
                            )

        changed_files_list = list(raw.get("changed_files") or [])
        deterministic_state = {
            "deterministic_changed_files": changed_files_list,
        }

        async def _run() -> None:
            nonlocal results, final_session
            last_error = None
            self._attempted_model_names = {self._normalize_model_name(self._current_model_name)}

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
                            state=deterministic_state,
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
                    if session is None:
                        raise RuntimeError("Failed to create deterministic ADK session")
                    if any(
                        session.state.get(key) != value
                        for key, value in deterministic_state.items()
                    ):
                        await self._session_service.append_event(
                            session,
                            Event(
                                invocation_id=trace_id,
                                author="webhook_agent_scope_gate",
                                actions=EventActions(state_delta=deterministic_state),
                            ),
                        )
                        session = await self._session_service.get_session(
                            app_name=self._app_name,
                            user_id=user_id,
                            session_id=session_id,
                        )

                    # Deduplication check: if a review was submitted < 30s ago, inject notice
                    if session and session.state:
                        last_review_ts = session.state.get("last_review_timestamp", 0)
                        now = time.time()
                        time_since_review = now - last_review_ts
                        if time_since_review < 30.0:
                            notice = (
                                f"\n\n[⚠️ SYSTEM NOTICE: You submitted a formal PR review {time_since_review:.1f} seconds ago. "
                                "Do NOT call review() again unless explicitly requested by a new /review command.]"
                            )
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + notice

                        previous_critique = session.state.get("last_review_critique", "")
                        if previous_critique:
                            critique_notice = (
                                f"\n\n[YOUR PREVIOUS REVIEW CRITIQUE]:\n{previous_critique}\n"
                                "Verify line-by-line which specific items were resolved by the new commit."
                            )
                            if user_message.parts and hasattr(user_message.parts[0], "text"):
                                user_message.parts[0].text = (
                                    user_message.parts[0].text or ""
                                ) + critique_notice

                    # Set user_state values - they get merged into session.state by InMemorySessionService
                    # This is needed because session copies are returned and our direct mutations wouldn't persist
                    user_state_map = self._session_service.user_state.setdefault(
                        self._app_name, {}
                    ).setdefault(user_id, {})
                    user_state_map["gh_client"] = gh_client
                    user_state_map["repo_full_name"] = repo_full_name
                    user_state_map["sender"] = user_id
                    user_state_map["review_mode"] = (
                        "sync" if canonical == "pull_request.synchronize" else "initial"
                    )
                    user_state_map["formal_review_eligible"] = _is_formal_review_eligible(
                        canonical,
                        comment_body,
                    )
                    if event_data.get("deterministic_precompiled_ast"):
                        user_state_map["deterministic_precompiled_ast"] = True
                        tools_exec = user_state_map.setdefault("tools_executed", [])
                        if "verify_python_ast" not in tools_exec:
                            tools_exec.append("verify_python_ast")
                        if "check_symbol_impact" not in tools_exec:
                            tools_exec.append("check_symbol_impact")
                    # Execute the ADK runner with current model
                    await _execute_agent()

                    # Fetch the final session state with all sub-agent output_key updates
                    final_session = await self._session_service.get_session(
                        app_name=self._app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    return  # Success - exit the retry loop

                except Exception as e:
                    if type(e).__name__ == "AbortAgentExecution" or "AbortAgentExecution" in str(
                        type(e)
                    ):
                        logger.info(
                            "🔒 Agent execution short-circuited (trace: %s): %s",
                            trace_id[-4:],
                            e,
                        )
                        results.append(
                            ActionResult(
                                tool="skip_closed_pr",
                                success=True,
                                detail=f"Execution short-circuited: {e}",
                            )
                        )
                        return

                    last_error = e
                    if _is_transient_error(e) and attempt < _MAX_RETRIES - 1:
                        rate_details = extract_rate_limit_details(e)
                        next_model = self._advance_model_chain(error=e)
                        if next_model is None:
                            break
                        err_s = str(e).lower()
                        is_503_high_demand = (
                            "503" in err_s or "unavailable" in err_s or "high demand" in err_s
                        )
                        parsed_retry = rate_details.get("retry_after_seconds")
                        if is_503_high_demand:
                            retry_delay = 0.5
                        elif parsed_retry is not None and parsed_retry > 0:
                            retry_delay = min(float(parsed_retry) + 0.5, 65.0)
                        else:
                            retry_delay = min(2.0 * (attempt + 1), 15.0)
                        logger.warning(
                            "Transient error on attempt %d/%d (trace: %s): %s. Active model failover -> %s (delay: %.1fs)",
                            attempt + 1,
                            _MAX_RETRIES,
                            trace_id[-4:],
                            e,
                            self._current_model_name,
                            retry_delay,
                        )
                        if retry_delay > 0:
                            await asyncio.sleep(retry_delay)
                        continue
                    logger.debug(
                        "Non-transient error or exhausted retries: raising exception (trace: %s)",
                        trace_id[-4:],
                    )
                    logger.exception(
                        "ADK agent run failed (trace: %s)",
                        trace_id[-4:],
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

        # Run the ADK coroutine on the persistent background loop to avoid
        # "Event loop is closed" issues when the process receives signals or
        # when httpx/anyio transports attempt to close transports on a loop
        # that has been shut down. This schedules the coroutine and waits
        # for completion synchronously.
        run_in_bg_loop(_run())

        # Deterministic review submission: if no review tool was called during a PR review event,
        # extract structured audit state from ADK session or emitted texts, and enforce verdict.
        canonical = event_data.get("canonical", "")
        raw = event_data.get("raw_payload", {})
        comment_body = (
            (raw.get("comment", {}) or {}).get("body", "") if isinstance(raw, dict) else ""
        )
        is_pr_review_event = _is_formal_review_eligible(canonical, comment_body)
        has_review_action = any(r.tool == "review" and r.success for r in results)

        if is_pr_review_event and not has_review_action:
            # 1. Safely extract review payload from session state or emitted text
            review_payload = ""
            if final_session and final_session.state:
                raw_analysis = final_session.state.get("code_review_analysis")
                raw_verdict = final_session.state.get("audit_verdict")

                if raw_analysis:
                    if hasattr(raw_analysis, "model_dump_json"):
                        review_payload = raw_analysis.model_dump_json()
                    elif isinstance(raw_analysis, dict):
                        import json

                        review_payload = json.dumps(raw_analysis)
                    else:
                        review_payload = str(raw_analysis)
                elif raw_verdict:
                    if hasattr(raw_verdict, "model_dump_json"):
                        review_payload = raw_verdict.model_dump_json()
                    elif isinstance(raw_verdict, dict):
                        import json

                        review_payload = json.dumps(raw_verdict)
                    else:
                        review_payload = str(raw_verdict)

            if not review_payload and emitted_texts:
                full_text = "\n\n".join(emitted_texts)
                is_review_content = (
                    "Scorecard" in full_text
                    or "| Category |" in full_text
                    or "Verdict:" in full_text
                    or '"verdict"' in full_text
                    or '"executive_summary"' in full_text
                    or '"critical_issues"' in full_text
                    or '"resolutions"' in full_text
                    or "Code Review" in full_text
                )
                if is_review_content:
                    review_payload = full_text

            if review_payload:
                pr_number = None
                if isinstance(raw, dict):
                    pr_number = (raw.get("pull_request") or {}).get("number") or (
                        raw.get("issue") or {}
                    ).get("number")

                if pr_number:
                    try:
                        repo = gh_client.get_repo(repo_full_name)
                        pr = repo.get_pull(pr_number)
                        detail, submitted = _submit_formal_review(
                            pr,
                            review_payload,
                            "COMMENT",
                            f"{repo_full_name}#{pr_number}",
                            {
                                "review_mode": (
                                    "sync" if canonical == "pull_request.synchronize" else "initial"
                                )
                            },
                        )
                        results.append(
                            ActionResult(
                                tool="review",
                                success=submitted,
                                detail=detail,
                            )
                        )
                        logger.info("Deterministic review result for PR #%d: %s", pr_number, detail)
                    except Exception as fallback_err:
                        logger.warning(
                            "Deterministic review submission failed: %s",
                            fallback_err,
                        )

        if not results:
            logger.info(
                "🏁 Agent completed with no actions (trace: %s)",
                trace_id[-4:],
            )

        return results
