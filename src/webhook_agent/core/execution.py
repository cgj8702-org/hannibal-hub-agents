"""Execution runner, retry orchestration, and writeback dispatch for WebhookAgent.

Extracted from core/agent_definition.py as part of Phase 3c modularization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from github import Github
from google.adk.events import Event, EventActions
from google.adk.runners import RunConfig
from google.genai import types as genai_types

from webhook_agent.core.loop_helpers import run_in_bg_loop
from webhook_agent.core.policy import Mode, resolve_event_policy
from webhook_agent.environment import pr_workspace, use_environment
from webhook_agent.models.model_chain import (
    _is_transient_error,
    _select_model_for_event,
)
from webhook_agent.models.model_factory import get_adk_model
from webhook_agent.models.rate_limiter import (
    _resolve_tier,
    extract_rate_limit_details,
    get_active_api_key,
)
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.writeback_policy import (
    _COMMENT_RATE_LIMITER,
)
from webhook_agent.state.review_checkpoint import review_checkpoint_manager
from webhook_agent.webhook_types import ActionResult

if TYPE_CHECKING:
    from webhook_agent.core.agent_definition import WebhookAgent

logger = logging.getLogger("webhook_agent.core.execution")

_MAX_RETRIES = int(
    os.environ.get("PRIMARY_MODEL_MAX_RETRIES") or os.environ.get("GEMMA_MODEL_MAX_RETRIES") or "10"
)


def _visible_text_parts(event: Any) -> list[str]:
    """Return the human-readable text parts of an ADK event, excluding thoughts.

    Args:
        event: An ADK ``Event`` (or a test double) emitted by ``runner.run_async``.

    Returns:
        Non-empty text payloads that are not flagged as model thought tokens.
    """
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return [
        part.text
        for part in parts
        if getattr(part, "text", None) and not getattr(part, "thought", False)
    ]


def _has_tool_calls(event: Any) -> bool:
    """Return True when an event carries function calls (a tool-working step).

    The ``hasattr(type(event), ...)`` guard keeps ``unittest.mock.MagicMock``
    doubles honest: they fabricate any attribute on access, so only a method
    that genuinely exists on the event's class is trusted.
    """
    getter = getattr(event, "get_function_calls", None)
    if callable(getter) and hasattr(type(event), "get_function_calls"):
        try:
            return bool(getter())
        except Exception:  # pragma: no cover - defensive against malformed events
            return False
    parts = getattr(getattr(event, "content", None), "parts", None) or []
    return any(
        hasattr(type(part), "function_call") and getattr(part, "function_call", None)
        for part in parts
    )


def _is_final_response_event(event: Any) -> bool:
    """Return True only for the model step that produces the user-facing answer.

    ADK emits one event per model step inside a single turn. Any step that ends
    in a tool call is an intermediate working step: its text is the model
    narrating what it is about to do ("Let's inspect ...", "Ah, excellent!").
    ADK flags those events as ``is_final_response() is False``; only the closing
    text-only step is final. Publishing the intermediate ones concatenates raw
    chain-of-thought into the GitHub comment.

    Args:
        event: An ADK ``Event`` (or a test double) emitted by ``runner.run_async``.

    Returns:
        True when the event's text represents a finished, user-facing answer.
    """
    if _has_tool_calls(event):
        return False
    # Streaming chunks are never a complete answer. `is True` rather than
    # truthiness: mock doubles fabricate truthy attributes, while a real
    # streaming part sets partial=True explicitly.
    if getattr(event, "partial", False) is True:
        return False
    final_check = getattr(event, "is_final_response", None)
    if final_check is not None and hasattr(type(event), "is_final_response"):
        try:
            # Tolerate both the historical method and a property-based helper.
            return bool(final_check() if callable(final_check) else final_check)
        except Exception:  # pragma: no cover - defensive against partial events
            return False
    return True


async def _execute_adk_runner(
    active_runner: Any,
    user_id: str,
    session_id: str,
    user_message: genai_types.Content,
    current_model_name: str,
    trace_id: str,
    results: list[ActionResult],
    emitted_texts: list[str],
    *,
    is_code_audit: bool = False,
    max_investigation_turns: int = 3,
) -> None:
    """Execute the ADK runner asynchronously and collect tool results and emitted text.

    For code PR audits (is_code_audit=True), enforces multi-turn repository investigation:
    if the model terminates Turn 1 without executing investigative tools (read_file, search_files, list_dir),
    it prompts for mandatory codebase exploration before accepting the final review.
    """
    max_llm_calls = int(
        os.environ.get("MAX_AUDITOR_LLM_CALLS") or os.environ.get("ADK_MAX_LLM_CALLS") or "15"
    )
    run_config = RunConfig(max_llm_calls=max_llm_calls)
    investigative_tools = {"read_file", "search_files", "list_dir"}

    async def _run_single_turn(msg: genai_types.Content) -> None:
        async for event in active_runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=msg,
            run_config=run_config,
        ):
            # Note: LLM token recording is handled by after_model_callback
            # to capture prompt, candidate, and thinking tokens accurately without double-counting.

            # Handle function response events — these are tool results from ADK
            if hasattr(event, "get_function_responses"):
                responses = event.get_function_responses()
                if responses:
                    logger.debug(
                        "🔧 Received %d tool responses from ADK",
                        len(responses),
                    )
                    for response in responses:
                        resp_data = response.response
                        is_success = True
                        if isinstance(resp_data, dict):
                            if "error" in resp_data or (
                                isinstance(resp_data.get("result"), str)
                                and resp_data["result"].startswith("Error:")
                            ):
                                is_success = False
                        elif isinstance(resp_data, str) and resp_data.startswith("Error:"):
                            is_success = False

                        results.append(
                            ActionResult(
                                tool=str(response.name or ""),
                                success=is_success,
                                detail=f"tool executed: {response.response}",
                                action_result=resp_data,
                            )
                        )

            # Handle text responses. Only the final, user-facing answer is written
            # back to GitHub; intermediate model steps narrate their plan and are
            # dropped here (see _is_final_response_event).
            text_parts = _visible_text_parts(event)
            if text_parts:
                if _is_final_response_event(event):
                    emitted_texts.extend(text_parts)
                    for text in text_parts:
                        logger.debug(
                            "🧠 Agent response: %s (trace: %s)",
                            text,
                            trace_id[-4:],
                        )
                else:
                    logger.debug(
                        "💭 Dropped intermediate narration from an unfinished model step "
                        "(trace: %s): %s",
                        trace_id[-4:],
                        " ".join(text_parts)[:500],
                    )

    try:
        await _run_single_turn(user_message)

        if is_code_audit:
            has_investigated = any(r.tool in investigative_tools for r in results)
            has_submitted_review = any(r.tool == "review" and r.success for r in results)
            if has_submitted_review:
                logger.info(
                    "🏁 [Investigation Gate] Review already submitted successfully (trace: %s); skipping exploration loop.",
                    trace_id[-4:],
                )
            turn = 1
            while (
                not has_investigated and not has_submitted_review and turn < max_investigation_turns
            ):
                turn += 1
                logger.info(
                    "🔍 [Investigation Gate] Model attempted review on code changes without "
                    "using investigative tools (trace: %s). Enforcing multi-turn exploration turn %d/%d...",
                    trace_id[-4:],
                    turn,
                    max_investigation_turns,
                )
                emitted_texts.clear()
                followup_directive = genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_text(
                            text=(
                                "Mandatory Investigation Gate: You cannot finalize a code review in a single turn without "
                                "active repository exploration. Use `search_files`, `list_dir`, or `read_file` to inspect callers, "
                                "directory layout, contract boundaries, and related test suites across the repository before providing your final review."
                            )
                        )
                    ],
                )
                await _run_single_turn(followup_directive)
                has_investigated = any(r.tool in investigative_tools for r in results)
                has_submitted_review = any(r.tool == "review" and r.success for r in results)
    except Exception as run_err:
        if "LlmCallsLimitExceededError" in type(run_err).__name__:
            logger.warning(
                "🛑 Auditor reached max LLM calls limit (%d calls) for trace %s: %s",
                max_llm_calls,
                trace_id[-4:],
                run_err,
                extra={
                    "json_fields": {
                        "event_type": "max_llm_calls_exceeded",
                        "max_llm_calls": max_llm_calls,
                        "trace_id": trace_id,
                        "error": str(run_err),
                    }
                },
            )
        else:
            raise


def _submit_deterministic_review(
    gh_client: Github,
    repo_full_name: str,
    pr_number: int | None,
    head_sha: str,
    canonical: str,
    final_session: Any,
    emitted_texts: list[str],
    results: list[ActionResult],
    *,
    force: bool = False,
    comment_body: str = "",
    is_pr: bool = True,
    model_name: str | None = None,
) -> None:
    """Submit formal code review extracted from session state or emitted texts."""
    if not is_pr:
        logger.debug(
            "Deterministic review skipped for %s#%s: target is an issue, not a pull request",
            repo_full_name,
            pr_number,
        )
        return

    review_payload = ""
    if final_session and final_session.state:
        raw_analysis = final_session.state.get("code_review_analysis")
        raw_verdict = final_session.state.get("audit_verdict")

        if raw_analysis:
            if hasattr(raw_analysis, "model_dump_json"):
                review_payload = raw_analysis.model_dump_json()
            elif isinstance(raw_analysis, dict):
                review_payload = json.dumps(raw_analysis)
            else:
                review_payload = str(raw_analysis)
        elif raw_verdict:
            if hasattr(raw_verdict, "model_dump_json"):
                review_payload = raw_verdict.model_dump_json()
            elif isinstance(raw_verdict, dict):
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

    if review_payload and pr_number:
        try:
            repo = gh_client.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)
            review_state = {
                "review_mode": ("sync" if canonical == "pull_request.synchronize" else "initial"),
                "is_explicit_review": force,
                "force_review": force,
                "comment_body": comment_body,
                "model_name": model_name,
            }
            if final_session and final_session.state:
                for k in (
                    "deterministic_contract_scorecard_item",
                    "deterministic_security_scorecard_item",
                    "deterministic_test_scorecard_item",
                ):
                    if k in final_session.state:
                        review_state[k] = final_session.state[k]
                    elif f"user:{k}" in final_session.state:
                        review_state[k] = final_session.state[f"user:{k}"]

            detail, submitted = _submit_formal_review(
                pr,
                review_payload,
                "COMMENT",
                f"{repo_full_name}#{pr_number}",
                review_state,
                analyzed_sha=head_sha,
                force=force,
                model_name=model_name,
            )
            results.append(
                ActionResult(
                    tool="review",
                    success=submitted,
                    detail=detail,
                )
            )
            logger.info(
                "Deterministic review result [repo=%s, item_id=%s, result=%s]",
                repo_full_name,
                pr_number,
                detail,
            )
            if submitted and head_sha:
                review_checkpoint_manager.mark_completed(repo_full_name, pr_number, head_sha)
            elif not submitted:
                logger.warning(
                    "Deterministic review was not submitted for PR #%d: %s",
                    pr_number,
                    detail,
                )
        except Exception as fallback_err:
            logger.warning(
                "Deterministic review submission failed: %s",
                fallback_err,
            )
    elif not review_payload and pr_number:
        logger.warning(
            "Deterministic review skipped for PR #%d: no structured review payload found",
            pr_number,
        )
        results.append(
            ActionResult(
                tool="review",
                success=False,
                detail="Skipped: no structured review payload found in session state or emitted texts.",
            )
        )


_CONVERSATIONAL_REPLY_STATE_KEY = "conversational_reply"


def _fresh_session_reply(final_session: Any, prior_state: dict[str, Any]) -> str:
    """Return the reply this run wrote to session state, or "" when there is none.

    ADK's ``output_key`` capture (``conversational_reply``) is an authoritative
    final answer: ``LlmAgent`` writes it only for events that satisfy
    ``is_final_response()`` and carry non-thought text, which is exactly the
    gate this runner applies when collecting emitted text. Reading it back is an
    independent second layer, mirroring the precedence the audit path already
    uses for ``code_review_analysis``.

    Comparing against the pre-run snapshot matters: session state is scoped to
    the PR thread, so a reply written by an earlier webhook would otherwise be
    reposted when the current run produced no answer of its own.

    Args:
        final_session: Session object fetched after the run, or None.
        prior_state: Session state captured immediately before the run.

    Returns:
        The fresh reply text, or "" if state holds no new answer.
    """
    if not final_session or not getattr(final_session, "state", None):
        return ""
    candidate = final_session.state.get(_CONVERSATIONAL_REPLY_STATE_KEY)
    if not isinstance(candidate, str) or not candidate.strip():
        return ""
    if candidate == prior_state.get(_CONVERSATIONAL_REPLY_STATE_KEY):
        return ""
    return candidate


def _post_conversational_reply(
    gh_client: Github,
    repo_full_name: str,
    pr_number: int | None,
    emitted_texts: list[str],
    trace_id: str,
    results: list[ActionResult],
    *,
    state_reply: str | None = None,
    model_name: str | None = None,
) -> None:
    """Post conversational reply comment to issue or PR thread.

    Prefers the session-state reply (ADK's own final-output capture) and falls
    back to text collected from final-response events.
    """
    if not pr_number:
        return

    full_reply = (state_reply or "").strip() or "\n\n".join(emitted_texts).strip()
    if not full_reply:
        return

    # Internal system notice leak prevention:
    # If the reply contains internal execution halt notices, strip them or suppress the reply entirely
    # to prevent leaking internal framework errors to public GitHub discussions.
    if "[⚠️ SYSTEM NOTICE: Execution halted" in full_reply:
        # Strip system notice lines if there is substantive user text, otherwise suppress completely
        cleaned_lines = [
            line
            for line in full_reply.splitlines()
            if "[⚠️ SYSTEM NOTICE: Execution halted" not in line
        ]
        full_reply = "\n".join(cleaned_lines).strip()
        if not full_reply:
            logger.warning(
                "⚠️ Suppressed conversational reply on %s#%s containing only internal system notice (%s)",
                repo_full_name,
                pr_number,
                trace_id[-4:],
            )
            return

    source = "session_state" if (state_reply or "").strip() else "emitted_texts"
    logger.debug(
        "💬 Reply source: %s (trace: %s, %d chars)",
        source,
        trace_id[-4:],
        len(full_reply),
    )

    target_key = f"{repo_full_name}#{pr_number}"
    if _COMMENT_RATE_LIMITER.is_allowed(target_key):
        try:
            repo = gh_client.get_repo(repo_full_name)
            issue = repo.get_issue(pr_number)
            comment_obj = issue.create_comment(full_reply)
            _COMMENT_RATE_LIMITER.record(target_key)
            results.append(
                ActionResult(
                    tool="add_comment",
                    success=True,
                    detail=f"Posted conversational comment to #{pr_number}: {getattr(comment_obj, 'html_url', 'OK')}",
                )
            )
            logger.info(
                "💬 Posted conversational comment [repo=%s, item_id=%s, model=%s, trace_id=%s]",
                repo_full_name,
                pr_number,
                model_name or "unknown",
                trace_id[-4:],
                extra={
                    "json_fields": {
                        "event": "conversational_comment_posted",
                        "repo": repo_full_name,
                        "item_id": pr_number,
                        "model": model_name or "unknown",
                        "trace_id": trace_id,
                        "url": getattr(comment_obj, "html_url", ""),
                    }
                },
            )
        except Exception as comment_err:
            logger.warning(
                "Failed to post conversational comment to #%d: %s",
                pr_number,
                comment_err,
            )
    else:
        logger.warning(
            "Conversational comment rate limited for %s (trace: %s)",
            target_key,
            trace_id[-4:],
        )


def execute_agent_event(
    agent: WebhookAgent,
    event_data: dict[str, Any],
    gh_client: Github,
    trace_id: str,
) -> list[ActionResult]:
    """Execute the ADK agent for an event, managing retries, checkpoints, and fallback submission."""
    repo_full_name = (
        event_data.get("repository", {}).get("full_name")
        if event_data.get("repository")
        else event_data.get("repo_name")
    )
    if not repo_full_name or repo_full_name == "unknown":
        raise ValueError("Cannot execute agent event: missing repository full_name")

    canonical = event_data.get("canonical", "")
    raw = event_data.get("raw_payload", {})
    if not isinstance(raw, dict):
        raise ValueError("Cannot execute agent event: raw_payload must be a dictionary")

    pr_number = (raw.get("pull_request") or {}).get("number") or (raw.get("issue") or {}).get(
        "number"
    )
    if not pr_number:
        raise ValueError(
            f"Cannot execute agent event: missing PR or issue number for repository '{repo_full_name}'"
        )

    is_pr_context = bool(raw.get("pull_request") or (raw.get("issue") or {}).get("pull_request"))
    head_sha = (
        str((raw.get("pull_request") or {}).get("head", {}).get("sha") or raw.get("after") or "")
        if is_pr_context
        else ""
    )

    sender = event_data.get("sender") or {}
    sender_login = sender.get("login", "") if isinstance(sender, dict) else str(sender or "")
    if not sender_login:
        raise ValueError("Cannot execute agent event: missing sender login")

    if gh_client is not None and "gh_client" not in event_data:
        event_data["gh_client"] = gh_client

    logger.debug(
        "✅ All policy checks passed, building session context (trace: %s)",
        trace_id[-4:],
    )

    # Derive session and user IDs via policy layer (PR-scoped, not sender-scoped)
    session_id = agent._derive_session_id(event_data)
    policy = resolve_event_policy(event_data, dry_run=agent.dry_run)
    user_id = policy.session_key

    if policy.mode == Mode.IGNORE:
        logger.info(
            "🛑 Event ignored by policy: %s (trace: %s)",
            policy.reason,
            trace_id[-4:],
        )
        return []

    logger.debug(
        "👤 Session context: session_id=%s, user_id=%s",
        session_id,
        user_id,
    )

    comment_body = (raw.get("comment", {}) or {}).get("body", "")

    results: list[ActionResult] = []

    # Snapshot guard: the prefetched diff reflects the live PR head, which may have
    # moved past the commit this event targets. Auditing a mixed pair (diff from
    # SHA B against a tree at SHA A) produces reviews about code nobody merged in
    # that shape, so defer and let the newer commit's own event run.
    diff_sha = str(raw.get("diff_sha") or "")
    if policy.mode == Mode.FORMAL_AUDIT and diff_sha and head_sha and diff_sha != head_sha:
        logger.warning(
            "⏭️ Deferring formal audit for %s#%s: diff snapshot %s does not match event head %s",
            repo_full_name,
            pr_number,
            diff_sha[:7],
            head_sha[:7],
        )
        results.append(
            ActionResult(
                tool="plan",
                success=False,
                detail=(
                    f"Deferred: prefetched diff is for {diff_sha[:7]} but the event targets "
                    f"{head_sha[:7]}; the newer commit will be reviewed on its own run."
                ),
            )
        )
        return results

    # Checkpoint gate: skip if commit has already been reviewed, or pause if rate-limited backoff active
    if policy.mode == Mode.FORMAL_AUDIT and pr_number and head_sha:
        checkpoint = review_checkpoint_manager.get_checkpoint(repo_full_name, pr_number, head_sha)
        if checkpoint and checkpoint.get("status") == "completed":
            logger.info(
                "🔒 Checkpoint: PR %s#%d at commit %s already reviewed (completed). Skipping.",
                repo_full_name,
                pr_number,
                head_sha,
            )
            results.append(
                ActionResult(
                    tool="review",
                    success=True,
                    detail=f"PR #{pr_number} already reviewed at commit {head_sha} (checkpoint completed)",
                )
            )
            return results

        if (
            checkpoint
            and checkpoint.get("status") == "rate_limited"
            and not event_data.get("resumed_checkpoint")
        ):
            resumable = review_checkpoint_manager.get_resumable(repo_full_name, pr_number, head_sha)
            if resumable is None:
                logger.info(
                    "⏸️ PR %s#%s review is paused due to rate limits; backoff window has not elapsed.",
                    repo_full_name,
                    pr_number,
                )
                results.append(
                    ActionResult(
                        tool="review",
                        success=True,
                        detail=f"PR #{pr_number} review paused (rate_limited, backoff active)",
                    )
                )
                return results
            event_data["resumed_checkpoint"] = resumable

    # Dynamic model routing
    selected_model = _select_model_for_event(event_data)
    if agent._current_model_name != selected_model:
        logger.info(
            "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
            selected_model,
            canonical,
            trace_id[-4:],
        )
        if hasattr(agent, "_switch_model"):
            agent._switch_model(selected_model)
        else:
            agent._current_model_name = selected_model
            new_model_instance = get_adk_model(
                model_name=selected_model,
                api_key=get_active_api_key(),
            )
            if hasattr(agent, "_code_auditor") and agent._code_auditor is not None:
                agent._code_auditor.model = new_model_instance
            if hasattr(agent, "_conversational_agent") and agent._conversational_agent is not None:
                agent._conversational_agent.model = new_model_instance

    emitted_texts: list[str] = []
    final_session = None
    prior_state: dict[str, Any] = {}

    changed_files_list = list(raw.get("changed_files") or [])
    doc_extensions = (
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
    )
    is_code_audit = policy.mode == Mode.FORMAL_AUDIT and any(
        not f.lower().endswith(doc_extensions) for f in changed_files_list
    )
    active_tier = event_data.get("tier") or _resolve_tier()
    deterministic_state = {
        "is_code_audit": is_code_audit,
        "model_name": agent._current_model_name,
        "active_model": agent._current_model_name,
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "issue_number": pr_number,
        "active_tier": active_tier,
        "deterministic_changed_files": changed_files_list,
        "deterministic_precompiled_ast": bool(event_data.get("deterministic_precompiled_ast")),
        "formal_review_eligible": policy.mode == Mode.FORMAL_AUDIT,
        "agent_mode": policy.mode,
        "deterministic_contract_scorecard_item": event_data.get(
            "deterministic_contract_scorecard_item"
        ),
        "deterministic_security_scorecard_item": event_data.get(
            "deterministic_security_scorecard_item"
        ),
        "deterministic_test_scorecard_item": event_data.get("deterministic_test_scorecard_item"),
    }

    user_message: genai_types.Content | None = None
    resumed_dossier = event_data.get("precompiled_dossier") or (
        event_data.get("resumed_checkpoint") or {}
    ).get("precompiled_dossier")
    if resumed_dossier:
        logger.info(
            "⚡ Fast-path: using stored precompiled dossier from review checkpoint (skipping AST/symbol pre-audit)"
        )
        user_message = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=str(resumed_dossier))],
        )
        event_data["deterministic_precompiled_ast"] = True
        deterministic_state["deterministic_precompiled_ast"] = True

    async def _run() -> None:
        nonlocal results, final_session, user_message
        last_error = None
        agent._attempted_model_names = {agent._normalize_model_name(agent._current_model_name)}

        if user_message is None:
            user_message = agent._build_user_message(event_data)
        if user_message is None:
            raise RuntimeError("Failed to build user message for execution")

        deterministic_state["deterministic_precompiled_ast"] = bool(
            event_data.get("deterministic_precompiled_ast")
        )
        for sc_key in (
            "deterministic_contract_scorecard_item",
            "deterministic_security_scorecard_item",
            "deterministic_test_scorecard_item",
        ):
            if sc_key in event_data:
                deterministic_state[sc_key] = event_data[sc_key]

        active_runner = (
            agent._runner if policy.mode == Mode.FORMAL_AUDIT else agent._conversational_runner
        )
        active_app_name = (
            getattr(getattr(active_runner, "app", None), "name", agent._app_name) or agent._app_name
        )

        base_prompt_text = (
            getattr(user_message.parts[0], "text", "") or ""
            if (user_message and user_message.parts)
            else ""
        )

        for attempt in range(_MAX_RETRIES):
            try:
                if user_message.parts and hasattr(user_message.parts[0], "text"):
                    user_message.parts[0].text = base_prompt_text
                active_runner = (
                    agent._runner
                    if policy.mode == Mode.FORMAL_AUDIT
                    else agent._conversational_runner
                )
                active_app_name = (
                    getattr(getattr(active_runner, "app", None), "name", agent._app_name)
                    or agent._app_name
                )
                session = await agent._session_service.get_session(
                    app_name=active_app_name,
                    user_id=user_id,
                    session_id=session_id,
                )

                # Snapshot the pre-run state so the session's `conversational_reply`
                # can be trusted as this run's answer only if the run rewrote it.
                # Taken on the first read, before any deterministic state append,
                # so bookkeeping writes can never masquerade as a fresh reply.
                prior_state.clear()
                if session is not None and session.state:
                    prior_state.update(session.state)

                if session is None:
                    await agent._session_service.create_session(
                        app_name=active_app_name,
                        user_id=user_id,
                        session_id=session_id,
                        state=deterministic_state,
                    )
                    session = await agent._session_service.get_session(
                        app_name=active_app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    logger.debug(
                        "Created new ADK session %s for user %s",
                        session_id,
                        user_id,
                    )
                if session is None:
                    raise RuntimeError("Failed to create deterministic ADK session")
                if any(
                    session.state.get(key) != value for key, value in deterministic_state.items()
                ):
                    await agent._session_service.append_event(
                        session,
                        Event(
                            invocation_id=trace_id,
                            author="webhook_agent_scope_gate",
                            actions=EventActions(state_delta=deterministic_state),
                        ),
                    )
                    session = await agent._session_service.get_session(
                        app_name=active_app_name,
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
                            user_message.parts[0].text = (user_message.parts[0].text or "") + notice

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

                from webhook_agent.core.commands import has_review_slash_command

                has_review_intent = is_pr_context and has_review_slash_command(comment_body)
                is_explicit_review = bool(
                    policy.is_explicit_request
                    or has_review_intent
                    or canonical == "pull_request_review_requested"
                )

                # Set user_state values
                user_state_map = agent._session_service.user_state.setdefault(
                    active_app_name, {}
                ).setdefault(user_id, {})
                user_state_map["gh_client"] = gh_client
                user_state_map["repo_full_name"] = repo_full_name
                user_state_map["pr_number"] = pr_number
                user_state_map["issue_number"] = pr_number
                user_state_map["active_tier"] = active_tier
                user_state_map["active_model"] = agent._current_model_name
                user_state_map["sender"] = sender_login
                user_state_map["review_mode"] = (
                    "sync" if canonical == "pull_request.synchronize" else "initial"
                )
                user_state_map["formal_review_eligible"] = policy.mode == Mode.FORMAL_AUDIT
                # Pin the analysed commit so writeback cannot stamp a review onto
                # a commit that landed while the audit was running.
                user_state_map["head_sha"] = head_sha
                user_state_map["comment_body"] = comment_body
                user_state_map["is_explicit_review"] = is_explicit_review
                user_state_map["force_review"] = is_explicit_review
                for sc_key in (
                    "deterministic_contract_scorecard_item",
                    "deterministic_security_scorecard_item",
                    "deterministic_test_scorecard_item",
                ):
                    if sc_key in event_data:
                        user_state_map[sc_key] = event_data[sc_key]

                if policy.mode == Mode.FORMAL_AUDIT and pr_number and head_sha:
                    delivery_id = str(event_data.get("delivery_id") or "")
                    installation_id = event_data.get("installation_id")
                    review_checkpoint_manager.save_checkpoint(
                        repo=repo_full_name,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        canonical=canonical,
                        precompiled_dossier=base_prompt_text,
                        pr_diff=str(raw.get("pr_diff") or ""),
                        diff_sha=str(raw.get("diff_sha") or ""),
                        changed_files=changed_files_list,
                        status="pending",
                        delivery_id=delivery_id,
                        sender=sender_login,
                        comment_body=comment_body,
                        installation_id=installation_id,
                    )

                # Execute runner
                await _execute_adk_runner(
                    active_runner=active_runner,
                    user_id=user_id,
                    session_id=session_id,
                    user_message=user_message,
                    current_model_name=agent._current_model_name,
                    trace_id=trace_id,
                    results=results,
                    emitted_texts=emitted_texts,
                    is_code_audit=is_code_audit,
                )

                # Fetch final session
                final_session = await agent._session_service.get_session(
                    app_name=active_app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
                return

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
                    est_prompt_tokens = (len(base_prompt_text) // 4) if base_prompt_text else None
                    model_before = agent._current_model_name
                    next_model = agent._advance_model_chain(
                        error=e, estimated_tokens=est_prompt_tokens
                    )
                    if next_model is None:
                        break
                    err_s = str(e).lower()
                    quota_type = rate_details.get("quota_type")
                    is_tpm = (
                        quota_type == "TPM"
                        or "token" in err_s
                        or "tpm" in err_s
                        or "token_count" in err_s
                    )
                    is_instant_failover = (
                        (next_model != model_before)
                        or "503" in err_s
                        or "unavailable" in err_s
                        or "high demand" in err_s
                        or is_tpm
                    )
                    parsed_retry = rate_details.get("retry_after_seconds")
                    if is_instant_failover:
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
                        agent._current_model_name,
                        retry_delay,
                        extra={
                            "json_fields": {
                                "event_type": "transient_model_error",
                                "attempt": attempt + 1,
                                "max_retries": _MAX_RETRIES,
                                "trace_id": trace_id,
                                "error": str(e),
                                "failover_model": agent._current_model_name,
                                "retry_delay": retry_delay,
                            }
                        },
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
            if pr_number and head_sha:
                rate_details = extract_rate_limit_details(last_error)
                retry_seconds = rate_details.get("retry_after_seconds")
                review_checkpoint_manager.mark_rate_limited(
                    repo=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    error_message=str(last_error),
                    retry_after_seconds=retry_seconds,
                )
            results.append(
                ActionResult(
                    tool="plan",
                    success=False,
                    detail=f"Model unavailable after {_MAX_RETRIES} retries: {last_error}",
                )
            )

    def _finalize_results() -> None:
        has_review_action = any(r.tool == "review" and r.success for r in results)

        is_comment_reconciliation = False
        if policy.mode != Mode.FORMAL_AUDIT and not has_review_action and emitted_texts:
            full_text = "\n\n".join(emitted_texts)
            has_resolutions = '"resolutions"' in full_text
            has_verdict = '"verdict"' in full_text
            is_pr_comment = canonical.startswith(
                ("issue_comment.", "pull_request_review_comment.", "pull_request_review.")
            )
            if is_pr_comment and has_resolutions and has_verdict:
                is_comment_reconciliation = True
                logger.info(
                    "Comment reconciliation detected (trace: %s): promoting to deterministic review submission",
                    trace_id[-4:],
                )

        has_investigated = any(r.tool in {"read_file", "search_files", "list_dir"} for r in results)
        if (
            policy.mode == Mode.FORMAL_AUDIT or is_comment_reconciliation
        ) and not has_review_action:
            if is_code_audit and not has_investigated:
                logger.warning(
                    "⚠️ [Investigation Gate] Code audit completed without any investigative tool calls (%s); suppressing ungrounded review writeback.",
                    trace_id[-4:],
                )
            else:
                from webhook_agent.core.commands import has_review_slash_command

                has_review_intent = is_pr_context and has_review_slash_command(comment_body)
                is_explicit_review = bool(
                    policy.is_explicit_request
                    or has_review_intent
                    or canonical == "pull_request_review_requested"
                )
                _submit_deterministic_review(
                    gh_client=gh_client,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    canonical=canonical,
                    final_session=final_session,
                    emitted_texts=emitted_texts,
                    results=results,
                    force=is_explicit_review,
                    comment_body=comment_body,
                    is_pr=is_pr_context,
                    model_name=agent._current_model_name,
                )
        elif policy.mode != Mode.FORMAL_AUDIT and not is_comment_reconciliation:
            _post_conversational_reply(
                gh_client=gh_client,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                emitted_texts=emitted_texts,
                trace_id=trace_id,
                results=results,
                state_reply=_fresh_session_reply(final_session, prior_state),
                model_name=agent._current_model_name,
            )

    async def _execute_with_workspace() -> None:
        nonlocal user_message
        if not is_pr_context or not head_sha:
            if user_message is None:
                user_message = agent._build_user_message(event_data)
                deterministic_state["deterministic_precompiled_ast"] = bool(
                    event_data.get("deterministic_precompiled_ast")
                )
                if "deterministic_contract_scorecard_item" in event_data:
                    deterministic_state["deterministic_contract_scorecard_item"] = event_data[
                        "deterministic_contract_scorecard_item"
                    ]
                if "deterministic_security_scorecard_item" in event_data:
                    deterministic_state["deterministic_security_scorecard_item"] = event_data[
                        "deterministic_security_scorecard_item"
                    ]
                if "deterministic_test_scorecard_item" in event_data:
                    deterministic_state["deterministic_test_scorecard_item"] = event_data[
                        "deterministic_test_scorecard_item"
                    ]
            await _run()
            _finalize_results()
            return

        async with pr_workspace(gh_client, repo_full_name, head_sha) as ws_env:
            if ws_env is not None:
                with use_environment(ws_env):
                    if user_message is None:
                        user_message = agent._build_user_message(event_data)
                        deterministic_state["deterministic_precompiled_ast"] = bool(
                            event_data.get("deterministic_precompiled_ast")
                        )
                        if "deterministic_contract_scorecard_item" in event_data:
                            deterministic_state["deterministic_contract_scorecard_item"] = (
                                event_data["deterministic_contract_scorecard_item"]
                            )
                        if "deterministic_security_scorecard_item" in event_data:
                            deterministic_state["deterministic_security_scorecard_item"] = (
                                event_data["deterministic_security_scorecard_item"]
                            )
                        if "deterministic_test_scorecard_item" in event_data:
                            deterministic_state["deterministic_test_scorecard_item"] = event_data[
                                "deterministic_test_scorecard_item"
                            ]
                        if user_message and user_message.parts:
                            logger.debug(
                                "📝 Built user message for agent (length: %d chars)",
                                len(getattr(user_message.parts[0], "text", "") or ""),
                            )
                    await _run()
                    _finalize_results()
            else:
                if user_message is None:
                    user_message = agent._build_user_message(event_data)
                    deterministic_state["deterministic_precompiled_ast"] = bool(
                        event_data.get("deterministic_precompiled_ast")
                    )
                    if "deterministic_contract_scorecard_item" in event_data:
                        deterministic_state["deterministic_contract_scorecard_item"] = event_data[
                            "deterministic_contract_scorecard_item"
                        ]
                    if "deterministic_security_scorecard_item" in event_data:
                        deterministic_state["deterministic_security_scorecard_item"] = event_data[
                            "deterministic_security_scorecard_item"
                        ]
                    if "deterministic_test_scorecard_item" in event_data:
                        deterministic_state["deterministic_test_scorecard_item"] = event_data[
                            "deterministic_test_scorecard_item"
                        ]
                    if user_message and user_message.parts:
                        logger.debug(
                            "📝 Built user message for agent (length: %d chars)",
                            len(getattr(user_message.parts[0], "text", "") or ""),
                        )
                await _run()
                _finalize_results()

    run_in_bg_loop(_execute_with_workspace())

    if not results:
        logger.info(
            "🏁 Agent completed with no actions [repo=%s, item_id=%s, trace_id=%s]",
            repo_full_name,
            pr_number,
            trace_id[-4:],
        )

    return results


__all__ = [
    "execute_agent_event",
]
