"""Review verdict enforcement, payload extraction, and guarded formal review submission.

Extracted from webhook_agent.py as Phase 3 of codebase modularization.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

from webhook_agent.analysis.diff_filter import filter_review_diff
from webhook_agent.review.critic import CriticEngine
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
from webhook_agent.review.hunter import HunterEngine
from webhook_agent.review.schemas import CodeReviewResponse, ScorecardItem, SyncReviewResponse
from webhook_agent.review.writeback_policy import _COMMENT_RATE_LIMITER, _review_lock
from webhook_agent.state.review_idempotency import review_claim_registry

logger = logging.getLogger("webhook_agent.review_enforcer")


def is_contract_dimension(dim: str) -> bool:
    """Check if a scorecard dimension corresponds to Dimension 4 (Contract Integrity)."""
    return "contract" in dim.strip().lower()


def is_security_dimension(dim: str) -> bool:
    """Check if a scorecard dimension corresponds to Dimension 3 (Security & Secrets)."""
    d = dim.strip().lower()
    return "security" in d or "secret" in d


def is_test_coverage_dimension(dim: str) -> bool:
    """Check if a scorecard dimension corresponds to Dimension 5 (Test Coverage & Regressions)."""
    d = dim.strip().lower()
    return "test" in d or "coverage" in d or "regression" in d


TOP_LEVEL_REVIEW_KEYS: frozenset[str] = frozenset(
    {
        "executive_summary",
        "summary",
        "scorecard",
        "critical_issues",
        "minor_suggestions",
        "resolutions",
        "candidate_findings",
        "verified_invariants",
    }
)


def matches_dimension(scorecard_dim: str, target_dim: str) -> bool:
    """Check if a scorecard dimension matches the target dimension."""
    s_dim = scorecard_dim.strip().lower()
    t_dim = target_dim.strip().lower()
    if s_dim == t_dim:
        return True
    if "contract" in t_dim and "contract" in s_dim:
        return True
    if ("security" in t_dim or "secret" in t_dim) and ("security" in s_dim or "secret" in s_dim):
        return True
    return bool(
        ("test" in t_dim or "coverage" in t_dim) and ("test" in s_dim or "coverage" in s_dim)
    )


def has_grounding_bypass_label(pr: Any) -> bool:
    """Check if the PR has the emergency owner bypass label (skip-grounding-gate)."""
    if pr is None:
        return False
    with contextlib.suppress(Exception):
        labels = getattr(pr, "labels", None)
        if labels is None and hasattr(pr, "get_labels"):
            labels = list(pr.get_labels())
        if labels:
            label_names = {(getattr(lbl, "name", "") or str(lbl)).strip().lower() for lbl in labels}
            return any(
                b in label_names
                for b in ("skip-grounding-gate", "skip-grounding", "bypass-grounding")
            )
    return False


def _handle_strict_grounding_gate(
    has_hard: bool,
    grounding_issues: list[Any],
    pr: Any,
    pr_label: str,
    enforced_verdict: str,
) -> str:
    """Apply strict grounding gate downgrade if hard issues exist and emergency bypass is not set."""
    from webhook_agent.review.grounding import hard_reasons, strict_grounding_enabled

    if not strict_grounding_enabled() or not has_hard:
        return enforced_verdict

    if has_grounding_bypass_label(pr):
        logger.info(
            "Strict Grounding Gate: Bypassed via emergency label 'skip-grounding-gate' on %s",
            pr_label,
        )
        return enforced_verdict

    logger.warning(
        "Strict Grounding Gate: Hard citation failures on %s: %s",
        pr_label,
        hard_reasons(grounding_issues),
    )
    if enforced_verdict == "APPROVE":
        logger.warning(
            "Strict Grounding Gate: Downgrading APPROVE to COMMENT due to ungrounded citations."
        )
        return "COMMENT"
    return enforced_verdict


def apply_scorecard_floor(
    scorecard: list[ScorecardItem],
    deterministic_item: ScorecardItem,
) -> list[ScorecardItem]:
    """Enforce deterministic floor on scorecard dimensions (Golden Asymmetry).

    The LLM has zero authority to overturn a deterministic FAIL or WARN.
    - If deterministic status is FAIL, the dimension status is strictly locked to FAIL.
    - If deterministic status is WARN, the dimension status cannot be upgraded to PASS or N/A.
    - If deterministic status is ERROR, the dimension status cannot be upgraded to PASS or N/A.
    - If the dimension was omitted by the LLM, the deterministic item is appended.
    """
    updated: list[ScorecardItem] = []
    found = False

    for item in scorecard:
        if matches_dimension(item.dimension, deterministic_item.dimension):
            found = True
            new_status = item.status
            new_notes = item.notes

            if deterministic_item.status == "FAIL":
                if item.status != "FAIL":
                    logger.warning(
                        "Deterministic Floor: Overriding LLM %s status on '%s' to FAIL: %s",
                        item.status,
                        item.dimension,
                        deterministic_item.notes,
                    )
                    new_status = "FAIL"
                    new_notes = deterministic_item.notes
            elif deterministic_item.status == "WARN":
                if item.status in ("PASS", "N/A"):
                    logger.warning(
                        "Deterministic Floor: Overriding LLM %s status on '%s' to WARN: %s",
                        item.status,
                        item.dimension,
                        deterministic_item.notes,
                    )
                    new_status = "WARN"
                    new_notes = deterministic_item.notes
            elif deterministic_item.status in ("ERROR", "UNKNOWN"):
                if item.status in ("PASS", "N/A"):
                    logger.warning(
                        "Deterministic Floor: Overriding LLM %s status on '%s' to %s: %s",
                        item.status,
                        item.dimension,
                        deterministic_item.status,
                        deterministic_item.notes,
                    )
                    new_status = deterministic_item.status
                    new_notes = deterministic_item.notes

            updated.append(
                ScorecardItem(
                    dimension=item.dimension,
                    status=new_status,
                    notes=new_notes,
                )
            )
        else:
            updated.append(item)

    if not found:
        updated.append(deterministic_item)

    return updated


def _apply_citation_grounding(
    review_obj: Any,
    diff_text: str,
    changed_paths: list[str] | None = None,
    pr_label: str = "",
) -> tuple[list[Any], bool]:
    """Verify citations against PR diff in shadow mode, logging telemetry.

    Returns (grounding_issues, has_hard_issues).
    """
    if not diff_text:
        return [], False

    from webhook_agent.review.grounding import (
        extract_grounding_findings,
        verify_citations,
    )

    findings = extract_grounding_findings(review_obj)
    if not findings:
        return [], False

    issues = verify_citations(findings, diff_text, changed_paths=changed_paths)
    if issues:
        issue_summary = [
            f"{getattr(i, 'severity', 'hard')}:{getattr(i, 'code', 'unknown')}" for i in issues
        ]
        logger.info(
            "🔎 [Grounding Telemetry] PR %s: %d ungrounded citation(s): %s",
            pr_label or "?",
            len(issues),
            issue_summary,
        )

    has_hard = any(getattr(i, "is_hard", False) for i in issues)
    return issues, has_hard


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
    body: str,
    event: str,
    pr: Any = None,
    review_mode: str | None = None,
    state: Any = None,
    deterministic_contract_item: ScorecardItem | None = None,
    deterministic_security_item: ScorecardItem | None = None,
    deterministic_test_item: ScorecardItem | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Programmatically enforce verdict rules based on structured JSON or Markdown text.

    Always parses and normalizes review output into strict Pydantic models (CodeReviewResponse
    or SyncReviewResponse), renders clean Markdown using render_code_review_markdown or
    render_sync_review_markdown, and generates native GitHub inline review comments with
    ```suggestion blocks for diff-anchored issues.

    Safety Invariant: A safety guardrail must ONLY downgrade verdicts (APPROVE -> REQUEST_CHANGES
    or APPROVE -> COMMENT), and must NEVER mechanically upgrade an intended REQUEST_CHANGES to APPROVE!
    """
    if not body or not body.strip():
        raise ValueError("Unparseable review payload: empty review body")

    from .comment_poster import build_github_review_comments

    det_contract_item: ScorecardItem | None = deterministic_contract_item
    det_security_item: ScorecardItem | None = deterministic_security_item
    det_test_item: ScorecardItem | None = deterministic_test_item
    head_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")

    if state is not None:
        state_dict_val = (
            state.to_dict()
            if hasattr(state, "to_dict")
            else (state if isinstance(state, dict) else {})
        )
        if det_contract_item is None:
            raw_det_sc = state_dict_val.get("deterministic_contract_scorecard_item")
            if isinstance(raw_det_sc, dict):
                with contextlib.suppress(Exception):
                    det_contract_item = ScorecardItem.model_validate(raw_det_sc)
            elif isinstance(raw_det_sc, ScorecardItem):
                det_contract_item = raw_det_sc

        if det_security_item is None:
            raw_sec_sc = state_dict_val.get("deterministic_security_scorecard_item")
            if isinstance(raw_sec_sc, dict):
                with contextlib.suppress(Exception):
                    det_security_item = ScorecardItem.model_validate(raw_sec_sc)
            elif isinstance(raw_sec_sc, ScorecardItem):
                det_security_item = raw_sec_sc

        if det_test_item is None:
            raw_test_sc = state_dict_val.get("deterministic_test_scorecard_item")
            if isinstance(raw_test_sc, dict):
                with contextlib.suppress(Exception):
                    det_test_item = ScorecardItem.model_validate(raw_test_sc)
            elif isinstance(raw_test_sc, ScorecardItem):
                det_test_item = raw_test_sc

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
            r"(\{[\s\S]*?\"(?:executive_summary|resolutions|critical_issues|minor_suggestions|candidate_findings)\"[\s\S]*?\})",
            body,
        ):
            json_candidates.append(json_obj_match.group(1))

        for cand in json_candidates:
            try:
                cand_data = json.loads(cand)
                if isinstance(cand_data, dict):
                    data_candidates.append(cand_data)
            except (json.JSONDecodeError, ValueError) as err:
                logger.debug("Failed parsing JSON candidate from review body: %s", err)
                continue

    has_prior_reviews = True
    diff_text = ""
    raw_diff = ""
    changed_paths: list[str] = []
    pr_label = f"#{getattr(pr, 'number', '?')}" if pr is not None else ""
    bot_reviews: list[Any] = []
    if pr is not None:
        from webhook_agent.github.bot_identity import is_hannibal_bot_login

        try:
            reviews = list(pr.get_reviews())
            bot_reviews = []
            for r in reviews:
                u = getattr(r, "user", None)
                login = (getattr(u, "login", "") or "").lower() if u else ""
                if is_hannibal_bot_login(login):
                    bot_reviews.append(r)
            has_prior_reviews = bool(bot_reviews)
        except Exception:
            has_prior_reviews = True
            bot_reviews = []

        review_budget: int | None = None
        existing_comments_data: list[dict[str, Any]] = []
        try:
            files = pr.get_files()
            patches: dict[str, str] = {}
            for f in files:
                fname = getattr(f, "filename", "")
                if fname:
                    patches[fname] = getattr(f, "patch", "") or ""
            changed_paths = list(patches.keys())
            from webhook_agent.review.grounding import diff_text_from_patches

            raw_diff = diff_text_from_patches(patches)
            filtered_res = filter_review_diff(raw_diff)
            diff_text = filtered_res.filtered_diff or raw_diff
            review_budget = filtered_res.budget
        except Exception as diff_err:
            logger.debug("Could not fetch PR diff text in _enforce_verdict: %s", diff_err)

        try:
            bot_review_comments: list[Any] = []
            for c in pr.get_review_comments():
                c_user = getattr(c, "user", None)
                c_login = (getattr(c_user, "login", "") or "").lower() if c_user else ""
                if is_hannibal_bot_login(c_login):
                    bot_review_comments.append(c)
                existing_comments_data.append(
                    {
                        "path": getattr(c, "path", ""),
                        "line": getattr(c, "line", None) or getattr(c, "original_line", None),
                        "body": getattr(c, "body", ""),
                    }
                )
            if review_budget is not None and bot_reviews:
                from webhook_agent.models.review_budget import (
                    compute_round_allowance,
                    summarise_review_history,
                )

                head_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")
                hist = summarise_review_history(bot_reviews, bot_review_comments, head_sha=head_sha)
                review_budget = compute_round_allowance(
                    round_number=hist.round_number,
                    previous_round_posted=hist.previous_round_posted,
                    total_posted_so_far=hist.total_posted_so_far,
                    initial_budget=review_budget,
                )
        except Exception as comm_err:
            logger.warning(
                "Could not fetch existing PR review comments or compute review budget: %s", comm_err
            )
    else:
        review_budget = None
        existing_comments_data = []

    if state is not None:
        state_dict_val = (
            state.to_dict()
            if hasattr(state, "to_dict")
            else (state if isinstance(state, dict) else {})
        )
        if not changed_paths and "deterministic_changed_files" in state_dict_val:
            changed_paths = list(state_dict_val["deterministic_changed_files"] or [])
        if not diff_text and "deterministic_diff_text" in state_dict_val:
            diff_text = str(state_dict_val["deterministic_diff_text"] or "")
        if not raw_diff and diff_text:
            raw_diff = diff_text

    critic_engine = CriticEngine()
    hunter_engine = HunterEngine()

    for data in data_candidates:
        try:
            if isinstance(data, dict):
                if not any(k in data for k in TOP_LEVEL_REVIEW_KEYS):
                    continue
                if is_intended_request_changes and not data.get("verdict"):
                    data["verdict"] = "REQUEST_CHANGES"

                # Process candidate findings through Hunter & Critic if present
                if "candidate_findings" in data and isinstance(data["candidate_findings"], list):
                    parsed_candidates = hunter_engine.parse_candidate_findings(
                        data["candidate_findings"]
                    )
                    verdicts = None
                    if "verification_verdicts" in data and isinstance(
                        data["verification_verdicts"], list
                    ):
                        verdicts = {}
                        for v in data["verification_verdicts"]:
                            if isinstance(v, dict) and "finding_id" in v:
                                from webhook_agent.review.schemas import VerificationVerdict

                                with contextlib.suppress(Exception):
                                    verdicts[v["finding_id"]] = VerificationVerdict.model_validate(
                                        v
                                    )
                    survivor_issues = critic_engine.filter_and_verify(
                        parsed_candidates, verdicts=verdicts
                    )
                    if "critical_issues" not in data and "minor_suggestions" not in data:
                        data = dict(data)
                        data["critical_issues"] = []
                        data["minor_suggestions"] = []
                        for issue in survivor_issues:
                            if getattr(issue, "severity", None) in ("CRITICAL", "HIGH"):
                                data["critical_issues"].append(issue)
                            else:
                                data["minor_suggestions"].append(issue)

                # Webhook mode is authoritative. The model may return the
                # initial schema for a synchronize event (or vice versa).
                if review_mode == "sync" and "summary" not in data:
                    data = dict(data)
                    data["summary"] = data.get("executive_summary")
                    if not data["summary"]:
                        continue
                    data.setdefault("resolutions", [])
                elif review_mode == "initial" and "executive_summary" not in data:
                    data = dict(data)
                    data["executive_summary"] = data.get("summary")
                    if not data["executive_summary"]:
                        continue

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
                    if det_contract_item is not None:
                        sync_obj.scorecard = apply_scorecard_floor(
                            sync_obj.scorecard, det_contract_item
                        )
                    if det_security_item is not None:
                        sync_obj.scorecard = apply_scorecard_floor(
                            sync_obj.scorecard, det_security_item
                        )
                    if det_test_item is not None:
                        sync_obj.scorecard = apply_scorecard_floor(
                            sync_obj.scorecard, det_test_item
                        )
                    sync_obj.critical_issues = critic_engine.filter_issues(sync_obj.critical_issues)
                    sync_obj.minor_suggestions = critic_engine.filter_issues(
                        sync_obj.minor_suggestions
                    )

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

                    diff_for_grounding = raw_diff or diff_text
                    if diff_for_grounding:
                        from webhook_agent.review.grounding import (
                            partition_invariants,
                            verify_resolutions,
                        )
                        from webhook_agent.review.schemas import SyncResolutionItem

                        grounded_res = verify_resolutions(sync_obj.resolutions, diff_for_grounding)
                        sync_obj.resolutions = [
                            SyncResolutionItem.model_validate(r) for r in grounded_res
                        ]
                        if sync_obj.verified_invariants:
                            v_invs, s_invs = partition_invariants(
                                sync_obj.verified_invariants,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                            )
                            sync_obj.verified_invariants = v_invs
                            for s in s_invs:
                                if s not in sync_obj.semantic_invariants:
                                    sync_obj.semantic_invariants.append(s)

                        if not sync_obj.verified_invariants and sync_obj.scorecard:
                            from webhook_agent.review.grounding import extract_scorecard_invariants

                            recovered_invariants = extract_scorecard_invariants(
                                sync_obj.scorecard,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                            )
                            if recovered_invariants:
                                sync_obj.verified_invariants = recovered_invariants

                        if sync_obj.semantic_invariants:
                            from webhook_agent.review.grounding import (
                                filter_and_ground_semantic_invariants,
                            )

                            grounded_sem, demoted_claims = filter_and_ground_semantic_invariants(
                                sync_obj.semantic_invariants,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                                head_sha=head_sha,
                            )
                            sync_obj.semantic_invariants = grounded_sem
                            for dc in demoted_claims:
                                if dc not in sync_obj.unverified_claims:
                                    sync_obj.unverified_claims.append(dc)

                    enforced_verdict = calculate_sync_verdict(sync_obj)
                    if diff_for_grounding:
                        grounding_issues, has_hard = _apply_citation_grounding(
                            sync_obj,
                            diff_for_grounding,
                            changed_paths=changed_paths,
                            pr_label=pr_label,
                        )
                        enforced_verdict = _handle_strict_grounding_gate(
                            has_hard=has_hard,
                            grounding_issues=grounding_issues,
                            pr=pr,
                            pr_label=pr_label,
                            enforced_verdict=enforced_verdict,
                        )

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
                    or "candidate_findings" in data
                ):
                    normalized_data = normalize_code_review_dict(data)
                    cr_obj = CodeReviewResponse.model_validate(normalized_data)
                    if det_contract_item is not None:
                        cr_obj.scorecard = apply_scorecard_floor(
                            cr_obj.scorecard, det_contract_item
                        )
                    if det_security_item is not None:
                        cr_obj.scorecard = apply_scorecard_floor(
                            cr_obj.scorecard, det_security_item
                        )
                    if det_test_item is not None:
                        cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_test_item)
                    cr_obj.critical_issues = critic_engine.filter_issues(cr_obj.critical_issues)
                    cr_obj.minor_suggestions = critic_engine.filter_issues(cr_obj.minor_suggestions)
                    enforced_verdict = calculate_strict_verdict(cr_obj)
                    diff_for_grounding = raw_diff or diff_text
                    if diff_for_grounding:
                        from webhook_agent.review.grounding import partition_invariants

                        if cr_obj.verified_invariants:
                            v_invs, s_invs = partition_invariants(
                                cr_obj.verified_invariants,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                            )
                            cr_obj.verified_invariants = v_invs
                            for s in s_invs:
                                if s not in cr_obj.semantic_invariants:
                                    cr_obj.semantic_invariants.append(s)

                        if not cr_obj.verified_invariants and cr_obj.scorecard:
                            from webhook_agent.review.grounding import extract_scorecard_invariants

                            recovered_invariants = extract_scorecard_invariants(
                                cr_obj.scorecard,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                            )
                            if recovered_invariants:
                                cr_obj.verified_invariants = recovered_invariants

                        if cr_obj.semantic_invariants:
                            from webhook_agent.review.grounding import (
                                filter_and_ground_semantic_invariants,
                            )

                            grounded_sem, demoted_claims = filter_and_ground_semantic_invariants(
                                cr_obj.semantic_invariants,
                                diff_for_grounding,
                                changed_paths=changed_paths,
                                head_sha=head_sha,
                            )
                            cr_obj.semantic_invariants = grounded_sem
                            for dc in demoted_claims:
                                if dc not in cr_obj.unverified_claims:
                                    cr_obj.unverified_claims.append(dc)

                        grounding_issues, has_hard = _apply_citation_grounding(
                            cr_obj,
                            diff_for_grounding,
                            changed_paths=changed_paths,
                            pr_label=pr_label,
                        )
                        enforced_verdict = _handle_strict_grounding_gate(
                            has_hard=has_hard,
                            grounding_issues=grounding_issues,
                            pr=pr,
                            pr_label=pr_label,
                            enforced_verdict=enforced_verdict,
                        )

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
        if not parsed_dict or not any(parsed_dict.get(k) for k in TOP_LEVEL_REVIEW_KEYS):
            raise ValueError(
                "Unparseable review payload: missing structured review content (no JSON review object or markdown review sections found)"
            )
        if is_intended_request_changes and not parsed_dict.get("verdict"):
            parsed_dict["verdict"] = "REQUEST_CHANGES"
        if review_mode == "sync" and "summary" not in parsed_dict:
            parsed_dict["summary"] = parsed_dict.get("executive_summary")
            if not parsed_dict["summary"]:
                raise ValueError("Unparseable review payload: sync review text missing summary")
            parsed_dict.setdefault("resolutions", [])
        elif review_mode == "initial" and "executive_summary" not in parsed_dict:
            parsed_dict["executive_summary"] = parsed_dict.get("summary")
            if not parsed_dict["executive_summary"]:
                raise ValueError(
                    "Unparseable review payload: initial review text missing executive_summary"
                )
        if review_mode == "sync":
            normalized_sync = normalize_sync_review_dict(parsed_dict)
            sync_obj = SyncReviewResponse.model_validate(normalized_sync)
            if det_contract_item is not None:
                sync_obj.scorecard = apply_scorecard_floor(sync_obj.scorecard, det_contract_item)
            if det_security_item is not None:
                sync_obj.scorecard = apply_scorecard_floor(sync_obj.scorecard, det_security_item)
            if det_test_item is not None:
                sync_obj.scorecard = apply_scorecard_floor(sync_obj.scorecard, det_test_item)
            sync_obj.critical_issues = critic_engine.filter_issues(sync_obj.critical_issues)
            sync_obj.minor_suggestions = critic_engine.filter_issues(sync_obj.minor_suggestions)
            if diff_text:
                from .comment_poster import filter_echo_suggestions

                sync_obj.minor_suggestions = filter_echo_suggestions(
                    sync_obj.minor_suggestions, diff_text
                )
            enforced_verdict = calculate_sync_verdict(sync_obj)
            diff_for_grounding = raw_diff or diff_text
            if diff_for_grounding:
                from webhook_agent.review.grounding import partition_invariants

                if sync_obj.verified_invariants:
                    v_invs, s_invs = partition_invariants(
                        sync_obj.verified_invariants,
                        diff_for_grounding,
                        changed_paths=changed_paths,
                    )
                    sync_obj.verified_invariants = v_invs
                    for s in s_invs:
                        if s not in sync_obj.semantic_invariants:
                            sync_obj.semantic_invariants.append(s)

                if not sync_obj.verified_invariants and sync_obj.scorecard:
                    from webhook_agent.review.grounding import extract_scorecard_invariants

                    recovered_invariants = extract_scorecard_invariants(
                        sync_obj.scorecard,
                        diff_for_grounding,
                        changed_paths=changed_paths,
                    )
                    if recovered_invariants:
                        sync_obj.verified_invariants = recovered_invariants

                if sync_obj.semantic_invariants:
                    from webhook_agent.review.grounding import (
                        filter_and_ground_semantic_invariants,
                    )

                    grounded_sem, demoted_claims = filter_and_ground_semantic_invariants(
                        sync_obj.semantic_invariants,
                        diff_for_grounding,
                        changed_paths=changed_paths,
                        head_sha=head_sha,
                    )
                    sync_obj.semantic_invariants = grounded_sem
                    for dc in demoted_claims:
                        if dc not in sync_obj.unverified_claims:
                            sync_obj.unverified_claims.append(dc)

                grounding_issues, has_hard = _apply_citation_grounding(
                    sync_obj,
                    diff_for_grounding,
                    changed_paths=changed_paths,
                    pr_label=pr_label,
                )
                enforced_verdict = _handle_strict_grounding_gate(
                    has_hard=has_hard,
                    grounding_issues=grounding_issues,
                    pr=pr,
                    pr_label=pr_label,
                    enforced_verdict=enforced_verdict,
                )

            rendered_body = render_sync_review_markdown(
                sync_obj, enforced_verdict, has_prior_reviews=has_prior_reviews
            )
            return rendered_body, enforced_verdict, []

        normalized_dict = normalize_code_review_dict(parsed_dict)
        cr_obj = CodeReviewResponse.model_validate(normalized_dict)
        if det_contract_item is not None:
            cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_contract_item)
        if det_security_item is not None:
            cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_security_item)
        if det_test_item is not None:
            cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_test_item)
        cr_obj.critical_issues = critic_engine.filter_issues(cr_obj.critical_issues)
        cr_obj.minor_suggestions = critic_engine.filter_issues(cr_obj.minor_suggestions)
        enforced_verdict = calculate_strict_verdict(cr_obj)
        diff_for_grounding = raw_diff or diff_text
        if diff_for_grounding:
            from webhook_agent.review.grounding import partition_invariants

            if cr_obj.verified_invariants:
                v_invs, s_invs = partition_invariants(
                    cr_obj.verified_invariants,
                    diff_for_grounding,
                    changed_paths=changed_paths,
                )
                cr_obj.verified_invariants = v_invs
                for s in s_invs:
                    if s not in cr_obj.semantic_invariants:
                        cr_obj.semantic_invariants.append(s)

            if not cr_obj.verified_invariants and cr_obj.scorecard:
                from webhook_agent.review.grounding import extract_scorecard_invariants

                recovered_invariants = extract_scorecard_invariants(
                    cr_obj.scorecard,
                    diff_for_grounding,
                    changed_paths=changed_paths,
                )
                if recovered_invariants:
                    cr_obj.verified_invariants = recovered_invariants

            if cr_obj.semantic_invariants:
                from webhook_agent.review.grounding import (
                    filter_and_ground_semantic_invariants,
                )

                grounded_sem, demoted_claims = filter_and_ground_semantic_invariants(
                    cr_obj.semantic_invariants,
                    diff_for_grounding,
                    changed_paths=changed_paths,
                    head_sha=head_sha,
                )
                cr_obj.semantic_invariants = grounded_sem
                for dc in demoted_claims:
                    if dc not in cr_obj.unverified_claims:
                        cr_obj.unverified_claims.append(dc)

            grounding_issues, has_hard = _apply_citation_grounding(
                cr_obj,
                diff_for_grounding,
                changed_paths=changed_paths,
                pr_label=pr_label,
            )
            enforced_verdict = _handle_strict_grounding_gate(
                has_hard=has_hard,
                grounding_issues=grounding_issues,
                pr=pr,
                pr_label=pr_label,
                enforced_verdict=enforced_verdict,
            )

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
        raise ValueError(f"Unparseable review payload: {parse_err}") from parse_err


def _submit_formal_review(
    pr: Any,
    body: str,
    event: str,
    target_key: str,
    state: Any = None,
    *,
    analyzed_sha: str = "",
) -> tuple[str, bool]:
    """Submit one guarded review, shared by the tool and deterministic fallback.

    Args:
        pr: PyGithub pull request object.
        body: Review payload (schema JSON rendered to markdown by the enforcer).
        event: Requested review event (APPROVE / COMMENT / REQUEST_CHANGES).
        target_key: ``owner/repo#number`` key used for locking and claims.
        state: Optional ADK state or dict carrying ``review_mode``.
        analyzed_sha: Commit the caller actually analysed. When the live head has
            moved past it, the review is deferred rather than stamped onto a
            commit it never saw.

    Returns:
        ``(status_message, submitted)``.
    """
    with _review_lock(target_key):
        live_sha = str(getattr(getattr(pr, "head", None), "sha", "") or "")
        requested_sha = analyzed_sha.strip()
        reviewed_sha = requested_sha or live_sha

        if requested_sha and live_sha and requested_sha != live_sha:
            logger.warning(
                "Deferring review for %s: head moved from %s to %s during the audit",
                target_key,
                requested_sha[:7],
                live_sha[:7],
            )
            return (
                f"Deferred: PR head moved to {live_sha[:7]} during the audit "
                f"(analysed {requested_sha[:7]}); the new commit will be reviewed on its own run.",
                False,
            )

        from webhook_agent.github.bot_identity import is_hannibal_bot_login

        reviews = list(pr.get_reviews())
        bot_reviews = []
        for rv in reviews:
            login = (getattr(getattr(rv, "user", None), "login", "") or "").lower()
            if is_hannibal_bot_login(login):
                bot_reviews.append(rv)

        if reviewed_sha and any(
            str(getattr(rv, "commit_id", "") or "") == reviewed_sha for rv in bot_reviews
        ):
            logger.info("Skipping duplicate bot review for %s at head %s", target_key, reviewed_sha)
            return f"Skipped: bot review already exists for current head {reviewed_sha}.", False

        claim = None
        if reviewed_sha:
            repo_name, _, number_text = target_key.rpartition("#")
            claim = review_claim_registry.claim(
                repo_name,
                int(number_text),
                reviewed_sha,
                github_review_exists=False,
            )
            if claim is None:
                return (
                    f"Skipped: review claim already exists for current head {reviewed_sha}.",
                    False,
                )

        state_dict = (
            state.to_dict()
            if hasattr(state, "to_dict")
            else (state if isinstance(state, dict) else {})
        )
        review_mode = state_dict.get("review_mode")
        if review_mode not in ("initial", "sync"):
            review_mode = "sync" if bot_reviews else "initial"
        try:
            rendered_body, enforced_event, inline_comments = _enforce_verdict(
                body, event, pr, review_mode=review_mode, state=state
            )
        except ValueError as parse_err:
            if claim is not None:
                review_claim_registry.release(claim)
            logger.error(
                "Cannot submit formal review for %s: %s (fast-failing review submission)",
                target_key,
                parse_err,
            )
            return f"Failed: {parse_err}", False

        # Re-anchor citations against the files this audit actually read. The
        # auditor counts lines by hand, so a two-line slip is routine even when the
        # quoted code is verbatim; the quote is the reliable half of the citation.
        from webhook_agent.review.citation_repair import repair_rendered_citations

        rendered_body = repair_rendered_citations(rendered_body, pr=pr)

        # Record the analysed commit in the review metadata so the body is
        # self-describing even after the PR moves on.
        if reviewed_sha:
            from webhook_agent.review.metadata import stamp_review_metadata

            rendered_body = stamp_review_metadata(rendered_body, sha=reviewed_sha)

        review_kwargs: dict[str, Any] = {"body": rendered_body, "event": enforced_event}
        if reviewed_sha:
            # GitHub stamps the review with the live head unless told otherwise.
            review_kwargs["commit"] = reviewed_sha
        try:
            if inline_comments:
                try:
                    rv = pr.create_review(comments=inline_comments, **review_kwargs)
                except Exception as review_err:
                    logger.warning(
                        "pr.create_review with %d inline comments failed (%s); falling back to body-only review",
                        len(inline_comments),
                        review_err,
                    )
                    rv = pr.create_review(**review_kwargs)
            else:
                rv = pr.create_review(**review_kwargs)
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

        # Auto-resolve bot review threads (e.g. CodeRabbit) on approved PR
        if enforced_event == "APPROVE":
            try:
                from webhook_agent.github.thread_resolver import (
                    auto_resolve_coderabbit_threads,
                    is_coderabbit_auto_resolve_enabled,
                )

                if is_coderabbit_auto_resolve_enabled():
                    resolved_threads = auto_resolve_coderabbit_threads(
                        repo_name, int(number_text), pr
                    )
                    if resolved_threads:
                        logger.info(
                            "Auto-resolved %d CodeRabbit review thread(s) on %s#%s: %s",
                            len(resolved_threads),
                            repo_name,
                            number_text,
                            resolved_threads,
                        )
            except Exception as auto_res_err:
                logger.warning(
                    "Auto-resolving review threads encountered non-fatal error on %s#%s: %s",
                    repo_name,
                    number_text,
                    auto_res_err,
                )

        detail = getattr(rv, "html_url", str(rv))
        return f"Submitted review ({enforced_event}): {detail}", True
