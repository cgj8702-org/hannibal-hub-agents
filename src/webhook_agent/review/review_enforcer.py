"""Review verdict enforcement, payload extraction, and guarded formal review submission.

Extracted from webhook_agent.py as Phase 3 of codebase modularization.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.review.formatter import (
    calculate_strict_verdict,
    calculate_sync_verdict,
    extract_json_payload,
    normalize_code_review_dict,
    normalize_sync_review_dict,
    parse_text_review_to_dict,
    render_code_review_markdown,
    render_sync_review_markdown,
)
from webhook_agent.review.schemas import CodeReviewResponse, SyncReviewResponse
from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER, _review_lock
from webhook_agent.state.review_idempotency import review_claim_registry

logger = logging.getLogger("webhook_agent.review_enforcer")


def _parse_scorecard_scores(body: str) -> list[int]:
    """Extract individual category scores from a review body's scorecard.

    Supports both Callout list format ('* **Category:** N/5') and table format ('| **Category** | N |').
    Returns a list of parsed integer scores, or empty list if none found.
    """
    scores: list[int] = []
    # Match callout list format: * **Code Correctness:** 5/5
    for match in re.finditer(r"\*\s*\*\*[^*]+\*\*\s*:\s*(\d)(?:/5)?", body):
        score = int(match.group(1))
        if 1 <= score <= 5:
            scores.append(score)

    if not scores:
        # Fallback to table format: | **Code Correctness** | 5 |
        for match in re.finditer(r"\|\s*\*\*[^*]+\*\*\s*\|\s*(\d)\s*\|", body):
            score = int(match.group(1))
            if 1 <= score <= 5:
                scores.append(score)
    return scores


def _enforce_verdict(
    body: str, event: str, pr: Any = None, review_mode: str | None = None
) -> tuple[str, str, list[dict[str, Any]]]:
    """Programmatically enforce verdict rules based on structured JSON or Markdown text.

    Always parses and normalizes review output into strict Pydantic models (CodeReviewResponse
    or SyncReviewResponse), renders clean Markdown using render_code_review_markdown or
    render_sync_review_markdown, and generates native GitHub inline review comments with
    ```suggestion blocks for diff-anchored issues.

    Safety Invariant: A safety guardrail must ONLY downgrade verdicts (APPROVE -> REQUEST_CHANGES
    or APPROVE -> COMMENT), and must NEVER mechanically upgrade an intended REQUEST_CHANGES to APPROVE!
    """
    from .comment_poster import build_github_review_comments

    cleaned_body = body.strip()
    req_event = (event or "").strip().upper()
    is_caller_request_changes = req_event == "REQUEST_CHANGES"
    is_body_request_changes = bool(
        re.search(
            r"##\s*(?:🛡️|⚡)?\s*Code Review(?:\s*Update)?:\s*`?REQUEST_CHANGES`?",
            body,
            re.IGNORECASE,
        )
    )
    is_intended_request_changes = is_caller_request_changes or is_body_request_changes

    # Step 1: Attempt robust JSON payload extraction, quote repair, and object salvage
    extracted_data = extract_json_payload(body)
    data_candidates: list[dict[str, Any]] = [extracted_data] if extracted_data else []

    if not data_candidates:
        # Step 2: Fallback candidate scanning
        json_candidates: list[str] = []
        if cleaned_body.startswith("```"):
            stripped_cb = re.sub(r"^```[a-z]*\n?", "", cleaned_body)
            stripped_cb = re.sub(r"\n?```$", "", stripped_cb).strip()
            json_candidates.append(stripped_cb)

        if cleaned_body.startswith("{") and cleaned_body.endswith("}"):
            json_candidates.append(cleaned_body)

        for cb_match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", body):
            json_candidates.append(cb_match.group(1))

        for json_obj_match in re.finditer(
            r"(\{[\s\S]*?\"(?:executive_summary|resolutions|critical_issues|minor_suggestions)\"[\s\S]*?\})",
            body,
        ):
            json_candidates.append(json_obj_match.group(1))

        for cand in json_candidates:
            try:
                cand_data = json.loads(cand)
                if isinstance(cand_data, dict):
                    data_candidates.append(cand_data)
            except Exception:
                continue

    has_prior_reviews = True
    diff_text = ""
    bot_reviews: list[Any] = []
    if pr is not None:
        try:
            reviews = list(pr.get_reviews())
            bot_reviews = []
            for r in reviews:
                u = getattr(r, "user", None)
                login = (getattr(u, "login", "") or "").lower() if u else ""
                if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                    bot_reviews.append(r)
            has_prior_reviews = bool(bot_reviews)
        except Exception:
            has_prior_reviews = True
            bot_reviews = []

        review_budget: int | None = None
        existing_comments_data: list[dict[str, Any]] = []
        try:
            files = pr.get_files()
            diff_lines: list[str] = []
            for f in files:
                patch = getattr(f, "patch", "") or ""
                diff_lines.append(f"+++ b/{f.filename}\n{patch}")
            raw_diff = "\n".join(diff_lines)
            filtered_res = filter_review_diff(raw_diff)
            diff_text = filtered_res.filtered_diff or raw_diff
            review_budget = filtered_res.budget
        except Exception as diff_err:
            logger.debug("Could not fetch PR diff text in _enforce_verdict: %s", diff_err)

        try:
            for c in pr.get_review_comments():
                existing_comments_data.append(
                    {
                        "path": getattr(c, "path", ""),
                        "line": getattr(c, "line", None) or getattr(c, "original_line", None),
                        "body": getattr(c, "body", ""),
                    }
                )
        except Exception as comm_err:
            logger.debug("Could not fetch existing PR review comments: %s", comm_err)
    else:
        review_budget = None
        existing_comments_data = []

    for data in data_candidates:
        try:
            if isinstance(data, dict):
                if is_intended_request_changes and not data.get("verdict"):
                    data["verdict"] = "REQUEST_CHANGES"

                # Webhook mode is authoritative. The model may return the
                # initial schema for a synchronize event (or vice versa).
                if review_mode == "sync" and "summary" not in data:
                    data = dict(data)
                    data["summary"] = data.get(
                        "executive_summary", "Pull request synchronization review update."
                    )
                    data.setdefault("resolutions", [])
                elif review_mode == "initial" and "executive_summary" not in data:
                    data = dict(data)
                    data["executive_summary"] = data.get(
                        "summary", "Autonomous PR code review report."
                    )

                use_sync_schema = review_mode == "sync" or (
                    review_mode not in ("initial", "sync")
                    and (
                        "resolutions" in data
                        or ("summary" in data and "executive_summary" not in data)
                    )
                )
                if use_sync_schema:
                    normalized_sync = normalize_sync_review_dict(data)
                    sync_obj = SyncReviewResponse.model_validate(normalized_sync)

                    # Programmatic hallucination guard: clear resolutions if no prior review
                    # had actionable items (critical issues, suggestions, or risks)
                    if pr is not None and sync_obj.resolutions:
                        from webhook_agent.review.metadata import has_actionable_findings

                        prior_had_findings = any(
                            has_actionable_findings(
                                getattr(r, "body", "") or "",
                                getattr(r, "state", ""),
                            )
                            for r in bot_reviews
                        )
                        if not prior_had_findings:
                            logger.warning(
                                "Resolution hallucination guard: Cleared %d fabricated resolutions (no prior actionable findings exist in bot reviews)",
                                len(sync_obj.resolutions),
                            )
                            sync_obj.resolutions = []

                    enforced_verdict = calculate_sync_verdict(sync_obj)
                    if is_intended_request_changes and enforced_verdict == "APPROVE":
                        logger.warning(
                            "Safety Guardrail: Prevented mechanical upgrade of sync REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
                        )
                        enforced_verdict = "REQUEST_CHANGES"
                    inline_comments: list[dict[str, Any]] = []
                    if diff_text:
                        from .comment_poster import (
                            build_github_review_comments,
                            filter_echo_suggestions,
                        )

                        sync_obj.minor_suggestions = filter_echo_suggestions(
                            sync_obj.minor_suggestions, diff_text
                        )
                        sync_issues = list(sync_obj.critical_issues) + list(
                            sync_obj.minor_suggestions
                        )
                        inline_comments, _ = build_github_review_comments(
                            sync_issues,
                            diff_text,
                            existing_comments=existing_comments_data,
                            max_comments=review_budget,
                        )
                    rendered_body = render_sync_review_markdown(
                        sync_obj, enforced_verdict, has_prior_reviews=has_prior_reviews
                    )
                    return rendered_body, enforced_verdict, inline_comments
                elif (
                    "executive_summary" in data
                    or "critical_issues" in data
                    or "minor_suggestions" in data
                ):
                    normalized_data = normalize_code_review_dict(data)
                    cr_obj = CodeReviewResponse.model_validate(normalized_data)
                    enforced_verdict = calculate_strict_verdict(cr_obj)
                    if is_intended_request_changes and enforced_verdict == "APPROVE":
                        logger.warning(
                            "Safety Guardrail: Prevented mechanical upgrade of REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
                        )
                        enforced_verdict = "REQUEST_CHANGES"
                    inline_comments = []
                    if diff_text:
                        from .comment_poster import (
                            build_github_review_comments,
                            filter_echo_suggestions,
                        )

                        cr_obj.minor_suggestions = filter_echo_suggestions(
                            cr_obj.minor_suggestions, diff_text
                        )
                        cr_issues = list(cr_obj.critical_issues) + list(cr_obj.minor_suggestions)
                        inline_comments, _ = build_github_review_comments(
                            cr_issues,
                            diff_text,
                            existing_comments=existing_comments_data,
                            max_comments=review_budget,
                        )
                    rendered_body = render_code_review_markdown(cr_obj, enforced_verdict)
                    return rendered_body, enforced_verdict, inline_comments
        except Exception as exc:
            logger.debug("Candidate JSON parse attempt skipped: %s", exc)

    # Step 2: Fallback text parsing if no valid JSON object was parsed
    try:
        parsed_dict = parse_text_review_to_dict(body)
        if is_intended_request_changes and not parsed_dict.get("verdict"):
            parsed_dict["verdict"] = "REQUEST_CHANGES"
        if review_mode == "sync" and "summary" not in parsed_dict:
            parsed_dict["summary"] = parsed_dict.get(
                "executive_summary", "Pull request synchronization review update."
            )
            parsed_dict.setdefault("resolutions", [])
        elif review_mode == "initial" and "executive_summary" not in parsed_dict:
            parsed_dict["executive_summary"] = parsed_dict.get(
                "summary", "Autonomous PR code review report."
            )
        if review_mode == "sync":
            normalized_sync = normalize_sync_review_dict(parsed_dict)
            sync_obj = SyncReviewResponse.model_validate(normalized_sync)
            if diff_text:
                from .comment_poster import filter_echo_suggestions

                sync_obj.minor_suggestions = filter_echo_suggestions(
                    sync_obj.minor_suggestions, diff_text
                )
            enforced_verdict = calculate_sync_verdict(sync_obj)
            rendered_body = render_sync_review_markdown(
                sync_obj, enforced_verdict, has_prior_reviews=has_prior_reviews
            )
            return rendered_body, enforced_verdict, []

        normalized_dict = normalize_code_review_dict(parsed_dict)
        cr_obj = CodeReviewResponse.model_validate(normalized_dict)
        enforced_verdict = calculate_strict_verdict(cr_obj)
        if is_intended_request_changes and enforced_verdict == "APPROVE":
            logger.warning(
                "Safety Guardrail: Prevented mechanical upgrade of text REQUEST_CHANGES to APPROVE! Enforcing REQUEST_CHANGES."
            )
            enforced_verdict = "REQUEST_CHANGES"
        inline_comments = []
        if diff_text:
            from .comment_poster import (
                build_github_review_comments,
                filter_echo_suggestions,
            )

            cr_obj.minor_suggestions = filter_echo_suggestions(cr_obj.minor_suggestions, diff_text)
            cr_issues = list(cr_obj.critical_issues) + list(cr_obj.minor_suggestions)
            inline_comments, _ = build_github_review_comments(
                cr_issues,
                diff_text,
                existing_comments=existing_comments_data,
                max_comments=review_budget,
            )
        rendered_body = render_code_review_markdown(cr_obj, enforced_verdict)
        return rendered_body, enforced_verdict, inline_comments
    except Exception as parse_err:
        logger.warning("Could not parse text review to CodeReviewResponse: %s", parse_err)

    fallback_event = req_event if req_event else "COMMENT"
    if is_intended_request_changes:
        fallback_event = "REQUEST_CHANGES"
    return body, fallback_event, []


def _submit_formal_review(
    pr: Any,
    body: str,
    event: str,
    target_key: str,
    state: Any = None,
) -> tuple[str, bool]:
    """Submit one guarded review, shared by the tool and deterministic fallback."""
    with _review_lock(target_key):
        head_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")
        reviews = list(pr.get_reviews())
        bot_reviews = []
        for rv in reviews:
            login = (getattr(getattr(rv, "user", None), "login", "") or "").lower()
            if "hannibal-hub-agents" in login or login.endswith("[bot]"):
                bot_reviews.append(rv)

        if head_sha and any(
            str(getattr(rv, "commit_id", "") or "") == head_sha for rv in bot_reviews
        ):
            logger.info("Skipping duplicate bot review for %s at head %s", target_key, head_sha)
            return f"Skipped: bot review already exists for current head {head_sha}.", False

        claim = None
        if head_sha:
            repo_name, _, number_text = target_key.rpartition("#")
            claim = review_claim_registry.claim(
                repo_name,
                int(number_text),
                head_sha,
                github_review_exists=False,
            )
            if claim is None:
                return f"Skipped: review claim already exists for current head {head_sha}.", False

        state_dict = (
            state.to_dict()
            if hasattr(state, "to_dict")
            else (state if isinstance(state, dict) else {})
        )
        review_mode = state_dict.get("review_mode")
        if review_mode not in ("initial", "sync"):
            review_mode = "sync" if bot_reviews else "initial"
        rendered_body, enforced_event, inline_comments = _enforce_verdict(
            body, event, pr, review_mode=review_mode
        )

        try:
            if inline_comments:
                try:
                    rv = pr.create_review(
                        body=rendered_body,
                        event=enforced_event,
                        comments=inline_comments,
                    )
                except Exception as review_err:
                    logger.warning(
                        "pr.create_review with %d inline comments failed (%s); falling back to body-only review",
                        len(inline_comments),
                        review_err,
                    )
                    rv = pr.create_review(body=rendered_body, event=enforced_event)
            else:
                rv = pr.create_review(body=rendered_body, event=enforced_event)
        except Exception:
            if claim is not None:
                review_claim_registry.release(claim)
            raise

        if claim is not None:
            review_claim_registry.mark_submitted(
                claim,
                str(getattr(rv, "id", "")),
                str(getattr(rv, "html_url", "")),
            )

        # Dismiss only after GitHub accepted the new review.
        for prev_rv in bot_reviews:
            if getattr(prev_rv, "state", "") in ("CHANGES_REQUESTED", "APPROVED") and getattr(
                prev_rv, "id", None
            ) != getattr(rv, "id", None):
                try:
                    prev_rv.dismiss("Superseded by fresh code review on latest commit.")
                except Exception as dismiss_err:
                    logger.warning(
                        "Could not dismiss prior bot review %s: %s", prev_rv.id, dismiss_err
                    )

        _COMMENT_RATE_LIMITER.record(target_key)
        if isinstance(state, dict):
            state["review_submitted_in_this_turn"] = True
        detail = getattr(rv, "html_url", str(rv))
        return f"Submitted review ({enforced_event}): {detail}", True
