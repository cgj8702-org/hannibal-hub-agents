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
from webhook_agent.models.model_chain import (
    _is_transient_error,
    _select_model_for_event,
)
from webhook_agent.models.model_factory import get_adk_model
from webhook_agent.models.rate_limiter import (
    extract_rate_limit_details,
    get_active_api_key,
    rpm_waiter,
)
from webhook_agent.review.review_enforcer import _submit_formal_review
from webhook_agent.review.writeback_policy import (
    _COMMENT_RATE_LIMITER,
    _is_formal_review_eligible,
)
from webhook_agent.state.review_checkpoint import review_checkpoint_manager
from webhook_agent.webhook_types import ActionResult

if TYPE_CHECKING:
    from webhook_agent.core.agent_definition import WebhookAgent

logger = logging.getLogger("webhook_agent.core.execution")

_MAX_RETRIES = int(
    os.environ.get("PRIMARY_MODEL_MAX_RETRIES") or os.environ.get("GEMMA_MODEL_MAX_RETRIES") or "5"
)


async def _execute_adk_runner(
    active_runner: Any,
    user_id: str,
    session_id: str,
    user_message: genai_types.Content,
    current_model_name: str,
    trace_id: str,
    results: list[ActionResult],
    emitted_texts: list[str],
) -> None:
    """Execute the ADK runner asynchronously and collect tool results and emitted text."""
    max_llm_calls = int(
        os.environ.get("MAX_AUDITOR_LLM_CALLS") or os.environ.get("ADK_MAX_LLM_CALLS") or "6"
    )
    run_config = RunConfig(max_llm_calls=max_llm_calls)
    try:
        async for event in active_runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
            run_config=run_config,
        ):
            # Handle token recording if usage metadata is available
            if hasattr(event, "usage_metadata") and event.usage_metadata:
                total_tok = getattr(event.usage_metadata, "total_token_count", 0) or getattr(
                    event.usage_metadata, "total_tokens", 0
                )
                if total_tok > 0:
                    await rpm_waiter.record_actual_tokens(
                        model=current_model_name,
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
    except Exception as run_err:
        if "LlmCallsLimitExceededError" in type(run_err).__name__:
            logger.warning(
                "🛑 Auditor reached max LLM calls limit (%d calls) for trace %s: %s",
                max_llm_calls,
                trace_id[-4:],
                run_err,
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
) -> None:
    """Submit formal code review extracted from session state or emitted texts."""
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
            detail, submitted = _submit_formal_review(
                pr,
                review_payload,
                "COMMENT",
                f"{repo_full_name}#{pr_number}",
                {"review_mode": ("sync" if canonical == "pull_request.synchronize" else "initial")},
            )
            results.append(
                ActionResult(
                    tool="review",
                    success=submitted,
                    detail=detail,
                )
            )
            logger.info("Deterministic review result for PR #%d: %s", pr_number, detail)
            if submitted and head_sha:
                review_checkpoint_manager.mark_completed(repo_full_name, pr_number, head_sha)
        except Exception as fallback_err:
            logger.warning(
                "Deterministic review submission failed: %s",
                fallback_err,
            )


def _post_conversational_reply(
    gh_client: Github,
    repo_full_name: str,
    pr_number: int | None,
    emitted_texts: list[str],
    trace_id: str,
    results: list[ActionResult],
) -> None:
    """Post conversational reply comment to issue or PR thread."""
    if not emitted_texts or not pr_number:
        return

    full_reply = "\n\n".join(emitted_texts).strip()
    if not full_reply:
        return

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
                "💬 Posted conversational comment to %s#%d (trace: %s)",
                repo_full_name,
                pr_number,
                trace_id[-4:],
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
        else "unknown"
    )
    canonical = event_data.get("canonical", "")
    raw = event_data.get("raw_payload", {})

    logger.debug(
        "✅ All policy checks passed, building session context (trace: %s)",
        trace_id[-4:],
    )

    # Derive session and user IDs
    session_id = agent._derive_session_id(event_data)
    sender = event_data.get("sender") or {}
    sender_login = sender.get("login", "") if isinstance(sender, dict) else str(sender or "")
    user_id = sender_login or "anonymous"

    logger.debug(
        "👤 Session context: session_id=%s, user_id=%s",
        session_id,
        user_id,
    )

    comment_body = (raw.get("comment", {}) or {}).get("body", "") if isinstance(raw, dict) else ""
    is_pr_review_event = _is_formal_review_eligible(canonical, comment_body)

    pr_number = None
    if isinstance(raw, dict):
        pr_number = (raw.get("pull_request") or {}).get("number") or (raw.get("issue") or {}).get(
            "number"
        )
    head_sha = (
        str((raw.get("pull_request") or {}).get("head", {}).get("sha") or raw.get("after") or "")
        if isinstance(raw, dict)
        else ""
    )

    results: list[ActionResult] = []

    # Checkpoint gate: skip if commit has already been reviewed, or pause if rate-limited backoff active
    if is_pr_review_event and pr_number and head_sha:
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

    # Build user message (fast-path from stored precompiled dossier on resume if present)
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
    else:
        user_message = agent._build_user_message(event_data)

    logger.debug(
        "📝 Built user message for agent (length: %d chars)",
        len(getattr(user_message.parts[0], "text", "") or "") if user_message.parts else 0,
    )

    # Dynamic model routing
    selected_model = _select_model_for_event(event_data)
    if agent._current_model_name != selected_model:
        logger.info(
            "🔀 Dynamic Model Router: assigned model %s for event '%s' (trace: %s)",
            selected_model,
            canonical,
            trace_id[-4:],
        )
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

    changed_files_list = list(raw.get("changed_files") or [])
    deterministic_state = {
        "deterministic_changed_files": changed_files_list,
    }

    async def _run() -> None:
        nonlocal results, final_session
        last_error = None
        agent._attempted_model_names = {agent._normalize_model_name(agent._current_model_name)}

        active_runner = agent._runner if is_pr_review_event else agent._conversational_runner
        active_app_name = (
            getattr(getattr(active_runner, "app", None), "name", agent._app_name) or agent._app_name
        )

        for attempt in range(_MAX_RETRIES):
            try:
                active_runner = (
                    agent._runner if is_pr_review_event else agent._conversational_runner
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
                    logger.info(
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

                # Set user_state values
                user_state_map = agent._session_service.user_state.setdefault(
                    active_app_name, {}
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
                if is_pr_review_event and pr_number and head_sha:
                    delivery_id = str(event_data.get("delivery_id") or "")
                    installation_id = event_data.get("installation_id")
                    review_checkpoint_manager.save_checkpoint(
                        repo=repo_full_name,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        canonical=canonical,
                        precompiled_dossier=getattr(user_message.parts[0], "text", "")
                        if user_message.parts
                        else "",
                        pr_diff=str(raw.get("pr_diff") or ""),
                        changed_files=changed_files_list,
                        status="pending",
                        delivery_id=delivery_id,
                        sender=user_id,
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
                    next_model = agent._advance_model_chain(error=e)
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
                        agent._current_model_name,
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

    run_in_bg_loop(_run())

    # Deterministic review submission
    has_review_action = any(r.tool == "review" and r.success for r in results)

    is_comment_reconciliation = False
    if not is_pr_review_event and not has_review_action and emitted_texts:
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

    if (is_pr_review_event or is_comment_reconciliation) and not has_review_action:
        _submit_deterministic_review(
            gh_client=gh_client,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            canonical=canonical,
            final_session=final_session,
            emitted_texts=emitted_texts,
            results=results,
        )
    elif not is_pr_review_event and not is_comment_reconciliation:
        _post_conversational_reply(
            gh_client=gh_client,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            emitted_texts=emitted_texts,
            trace_id=trace_id,
            results=results,
        )

    if not results:
        logger.info(
            "🏁 Agent completed with no actions (trace: %s)",
            trace_id[-4:],
        )

    return results


__all__ = [
    "execute_agent_event",
]
