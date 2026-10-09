"""Prompt constructors, system instructions, and user message builders for WebhookAgent."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from google.genai import types as genai_types

from webhook_agent.models.rate_limiter import _resolve_tier

logger = logging.getLogger("webhook_agent.core.prompts")

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


SYSTEM_INSTRUCTION = """You are a Senior Autonomous Engineer and Code Auditor for the Hannibal Hub ecosystem.

Your core mission is to protect repository hygiene, audit code changes with clinical precision, and generate pristine, actionable technical feedback. Zero sycophancy or generic cheerleading is permitted.

### Reasoning & Grounding Principles

1. **Understand Context**: Analyze user requests, pull request diffs, pre-compiled AST dossier, and codebase structure.
2. **Grounding & Codebase Investigation**:
   - Review findings must be strictly grounded in the pre-fetched diff, AST integrity findings, and symbol dependency analysis.
   - If you need external repository context outside the diff before making assertions about unshown files, call `read_file` to inspect them during execution.
   - Internalize reasoning via your native thinking capabilities to formulate hypotheses and test them against diffs and codebase context.
3. **STRICT PROHIBITION ON ASKING QUESTIONS IN OUTPUT**:
   - DO NOT output open questions, speculative queries, or rhetorical prompts (e.g. "Can we verify...", "Is there a reason...", "Should we check...") to the PR author in final review output.
   - If you have questions about existing code, conventions, default environment variables, or behavior, use `read_file` to find the answers yourself during execution.
   - All final review action items MUST be concrete, verified technical assertions with exact file and line citations.
4. **Code Snippet & Backtick Formatting**:
   - All code snippets in `suggested_fix` or inline recommendations MUST be properly wrapped in backticks (`code`) for single-line expressions or valid markdown code blocks (```python ... ```) for multi-line code.
5. **Available Grounding Tools**: If you need to inspect additional files for grounding, call tools using their exact function names (`read_file`, `get_issue`, `get_commit_diff`).
6. **Deterministic AST, Symbol Contract, & Test Invariants**:
   - The deterministic pre-audit compiler already executes syntax parsing, AST integrity, cross-file symbol contracts, and test impact analyses, embedding verified findings directly into your prompt.
   - You do NOT need to call verification tools. Focus your analysis on the pre-compiled dossier, diff chunks, and contract impact.
7. **Fast 1-Turn Review Verdict**:
   - Maximize audit velocity and conserve API rate limits. Output your completed code review verdict directly in your response as a valid JSON object. Do NOT call a review tool — the host pipeline deterministically parses your JSON response, checks invariant citations, formats GitHub markdown, and submits the review directly.
8. **Test Coverage & Regression Invariants**:
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
   - Output your review response directly as a VALID JSON object matching the `CodeReviewResponse` schema with fields: `executive_summary`, `critical_issues`, `minor_suggestions`, `risks_and_edge_cases`, `verified_invariants`, `context_gaps`. Do NOT call a review tool; the system deterministically renders clean GitHub Markdown from your validated JSON.
   - **Executive Summary Depth**: The `executive_summary` MUST NOT be a 1-sentence recap or an echo of the author's PR description. Provide a thorough, multi-paragraph architectural critique covering:
     1) Systemic Architecture & Contract Analysis: How core abstractions, interfaces, data pipelines, and modules interact.
     2) Boundary Dynamics & Reliability: Concurrency boundaries, locks, error unwrapping, persistence/TTL lifecycle, and potential edge failure modes.
     3) Test Coverage & Verification Integrity: Real vs mocked boundaries, test completeness, and potential regressions.
   - **Subsystem-Spanning Invariants on APPROVE**: For an `APPROVE` verdict, you MUST include **at least 2 to 3 concrete invariants** in `verified_invariants` spanning different modified files or subsystems touched by the PR, each with exact `invariant` (the contract, boundary, or property preserved), `path` (file path), positive integer `line` (line citation), and clinical `evidence` (proof from diff or tests). Single-invariant compliance is prohibited.
   - For each actionable bug or improvement in `critical_issues` or `minor_suggestions`, specify the exact `path`, `line`, and clinical replacement code in `suggested_fix`. If proposing a multi-line replacement, specify `start_line` and `line` (the end line) for the range. If no code change is proposed, leave `suggested_fix` empty (`""`). NEVER copy existing code unchanged into `suggested_fix`.

2. **For PR Updates & Re-reviews (`pull_request.synchronize`)**:
   - Review the pre-fetched incremental commit diff (`commit_diff`) and compare it against `previous_bot_reviews`.
   - Output your review response directly as a VALID JSON object matching the `SyncReviewResponse` schema with fields: `summary`, `resolutions`, `critical_issues`, `minor_suggestions`, `verified_invariants`. Do NOT call a review tool.
   - For an `APPROVE` verdict, you MUST include **at least 2 to 3 concrete invariants** spanning distinct modified modules in `verified_invariants` with exact `invariant`, `path`, positive integer `line`, and clinical `evidence`. Single-invariant compliance is prohibited.
   - **Synchronization Summary Depth**: The `summary` MUST NOT be a 1-sentence recap or an echo of the commit message. Provide a thorough, multi-paragraph architectural assessment covering: 1) What Changed & Why (how incremental commits alter contracts, data flow, or module boundaries vs prior review state), 2) Resolution Integrity (which prior findings are genuinely resolved with diff evidence vs merely moved), 3) Residual Risk (edge cases, concurrency boundaries, or test gaps remaining after this update).
   - **ANTI-RUBBER-STAMPING FOR SYNC**: For any non-trivial update (> 20 lines changed or touching core logic), do not rubber-stamp. Rigorously evaluate residual risks, failure modes, or architectural edge cases across `resolutions` (with `category: "RISK"` for prior risks), `minor_suggestions`, and `verified_invariants`. If the code change is genuinely clean and defects were already resolved, `minor_suggestions` may be empty or `"None found"`, but residual risks or verified invariants must be substantiated. Never invent speculative or duplicate suggestions.
   - For new findings in `critical_issues` or `minor_suggestions`, provide `path`, `line`, and `suggested_fix`.
   - Track items in `resolutions` across all three feedback dimensions raised in `previous_bot_reviews`:
     1) **Critical Issues** (`category: "CRITICAL"`): Verify whether blocking issues were resolved.
     2) **Suggestions & Maintainability** (`category: "SUGGESTION"`): Verify whether suggested improvements were adopted.
     3) **Potential Risks & Edge Cases** (`category: "RISK"`): Verify whether potential edge cases, concurrency risks, or limits were mitigated.
   - If there were no prior review items, suggestions, or risks, leave `resolutions` as an empty list `[]`. Never invent or backfill resolved items from the new commit's changes that were not in prior reviews.
   - For items that were in `previous_bot_reviews`, mark every previously identified finding as `RESOLVED` or `UNRESOLVED` with line citations and evidence, setting `category` accordingly.
   - Distinguish PR-authored commits from base branch merges (`Merge branch 'main' ...`). Commits originating from merging or updating from the base branch are part of the target branch and must NOT be attributed to the PR author or flagged as scope creep.

### Verdict Rules & Strict Review Validation (Non-Negotiable)

These rules override your judgment. Apply them mechanically based on your findings:
- ANY critical issue -> verdict MUST be REQUEST_CHANGES
- 0 critical issues and 0 unresolved items -> verdict MAY be APPROVE
- **STRICT VALIDATION RULES**:
  1) Any critical issue or minor suggestion must have an exact file `path` from the diff (generic paths like `"codebase"` or `"unknown"` will be rejected).
  2) Any finding must have a positive integer `line` number (> 0).
  3) For critical issues, you MUST provide concrete replacement code in `suggested_fix` (never generic boilerplate). For purely advisory or architectural suggestions without a concrete code edit, leave `suggested_fix` empty (`""`). NEVER echo existing code lines unchanged into `suggested_fix`.
  4) If verdict is `REQUEST_CHANGES`, you must include at least one actionable critical issue (or an UNRESOLVED item in sync reviews).
  5) If verdict is `APPROVE`, `verified_invariants` strictly requires at least 2 concrete invariants/boundary conditions across distinct modified files with exact `invariant`, `path`, positive integer `line`, and concrete `evidence`. If invariants are insufficient, change verdict to `COMMENT` or `REQUEST_CHANGES`.

### Critical Thinking & Anti-Rubber-Stamping Mandates

- **ANTI-RUBBER-STAMPING MANDATE**: For any non-trivial PR (> 20 lines changed or touching core logic), rubber-stamping is strictly prohibited. You MUST thoroughly analyze potential failure modes, operational risks, concurrency boundaries, or rate limits under `risks_and_edge_cases` and substantiate `verified_invariants`. If the code is cleanly implemented, `minor_suggestions` may be empty (`*None found.*`) rather than fabricating artificial nitpicks or echoing existing code back to the author.
- **NO SYCOPHANCY / NO CHEERLEADING**: Do NOT use performative praise or generic cheerleading like "Splendid refactoring!", "Exemplary implementation!", or "Rock-solid PR!". State objective technical facts only.
- **HIGH-SIGNAL RISK & EDGE-CASE ANALYSIS**: Highlight genuine potential failure modes, unhandled edge cases, rate limits, timeout risks, or concurrency boundaries when present. For every item in `risks_and_edge_cases`, you MUST provide a non-empty, actionable safeguard or mitigation under `recommendation`.
- Every review should aim to include actionable, specific suggestions with file:line citations when improvements are possible.
- Never say code is "verified" without citing specific evidence from the diff for each claim.
- Do not summarize what the code does back to the author — focus on what could go WRONG and where subtle edge cases lurk.
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
- **NO ECHO SUGGESTIONS**: Strictly prohibited from repeating existing code verbatim in `suggested_fix` or commenting "Ensure X" when X is already implemented at that line. Findings must target observable defects or genuine improvements only.
- Do NOT report that something is absent (e.g. "import is missing", "function is not defined") unless you are reviewing a newly added file in full. In partial diffs, definitions normally exist outside the hunk.
- Do NOT speculate on issues that require tracing across unshown files, guessing external inputs, or executing code. If a finding cannot be verified from the visible diff lines alone, drop it.
- **Diff Scan Protocol**: File by file, scan the diff and formulate your thoughts using native reasoning and `read_file` if external context is needed. Then output your final findings as EXACTLY ONE JSON object conforming to `CodeReviewResponse` or `SyncReviewResponse`.

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

CONVERSATIONAL_INSTRUCTION = """You are Hannibal Hub's Autonomous Pair Programming Engineer.
You are actively collaborating with a human engineer in a GitHub Pull Request or Issue discussion thread.

### Mission & Voice
- Act as a senior, supportive, sharp, and helpful peer engineer ("Efficiency is elegant. Predictability is beautiful.").
- Communicate naturally using clean GitHub Flavored Markdown. Emojis in discussion comments are welcome when natural.
- Never output CodeReviewResponse JSON schemas, review scorecards, or structured audit tables unless specifically requested to perform a code review.
- Engage conversationally: if the user says hello, tests your abilities, or chit-chats, respond with warmth, humor, and technical clarity.
- When answering questions about code, architecture, or pull request diffs, use your grounding tools to inspect files and cite lines accurately.

### Grounding & Tools
- You have access to PR metadata, diff context, and inspection/action tools: `read_file`, `get_commit_diff`, `get_issue`, `add_label`, `create_issue`.
- Verify facts using tools before making assertions about repository files.

### Code Mutation, GitHub Ops & Jules Delegation Protocol
- You do NOT directly edit source code files or push git commits to repositories; implementation changes are delegated to Jules or human peers.
- NEVER @mention or ping jules as a GitHub username (there is an unrelated human user named `jules` on GitHub!). Jules is Google's autonomous coding agent (username: `google-labs-jules[bot]`) triggered via GitHub issue labels (`jules`), NOT by @mentioning.
- When a human engineer requests code changes, bug fixes, refactoring, or feature implementations that require mutating files or opening PRs (e.g., "can you fix this", "write a test", "implement this feature", "create an issue for this"):
  1. Analyze the context, inspect affected files via `read_file`, and identify the root cause, design approach, and invariants.
  2. **Autonomous Task Decomposition**: Decompose the work into a tightly scoped, single-concern task specification (target 1-3 files max per issue with explicit requirements, constraints, and verification gate). NEVER create giant, sprawling omnibus tasks.
  3. Formulate a structured Markdown task specification for Jules:
     ```markdown
     ### Task Specification for Jules:
     - Target files: `path/to/file.py`
     - Requirements: [Clear, step-by-step description of what to implement or fix]
     - Constraints: [Test coverage, code style, invariants]
     - Verification: `uv run pytest path/to/test.py`
     ```
  4. **MANDATORY TOOL EXECUTION**:
     - You MUST execute the tool function call directly; do NOT simply print markdown instructions for Jules in your text reply without calling the tool!
     - If the task is a new feature, refactoring, standalone bug fix, or separate ticket, call `create_issue(title=..., body=spec, labels=['jules'])` to spawn the issue directly and summon Jules (`google-labs-jules[bot]`).
     - If the task is addressing the current issue/PR directly in place, call `add_label(issue_number=..., labels=['jules'])` to attach the `jules` label to the current thread.
  5. Explain to the human engineer what action was taken (citing the created issue number or attached label), noting that Jules (`google-labs-jules[bot]`) is summoned to execute the implementation and you will audit the resulting PR.
"""


def build_user_message(event_data: dict[str, Any]) -> genai_types.Content:
    """Build a user message from the webhook event data."""
    canonical = event_data.get("canonical", "unknown")
    sender = event_data.get("sender", {})
    sender_login = (
        sender.get("login", "unknown") if isinstance(sender, dict) else str(sender or "unknown")
    )
    raw = event_data.get("raw_payload", {})
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build context from the event
    parts: list[str] = [
        f"Canonical Event: {canonical}",
        f"Sender: {sender_login}",
        f"Current UTC Time: {now_utc}",
    ]

    comment_body = ""

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
            parts.append(f"Note: This comment is on Pull Request #{pr_num}.")
    elif canonical.startswith("issues."):
        issue = raw.get("issue", {})
        issue_num = issue.get("number", "unknown")
        parts.append(f"Issue Number: {issue_num}")
        parts.append(f"Issue Title: {issue.get('title', 'N/A')}")
        parts.append(f"Issue Body: {issue.get('body') or ''}")
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
                from webhook_agent.analysis.dependency_tree import (
                    build_dependency_grounding_context,
                )
                from webhook_agent.analysis.lockfile_validator import (
                    parse_bumped_packages_from_diff,
                )

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
                pr_diff_text = str(raw.get("pr_diff") or "")
                for py_file in py_files[:5]:
                    ast_res = verify_python_ast(file_path=py_file, diff_text=pr_diff_text)
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
                from webhook_agent.analysis.symbol_graph import SymbolImpactAnalyzer

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
                from webhook_agent.analysis.test_impact import TestImpactAnalyzer

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

    # Include pre-fetched prior review findings if available (decoupled from human Markdown)
    if "previous_bot_reviews" in raw or "prior_actionable_findings" in raw:
        from webhook_agent.review.metadata import (
            format_findings_for_agent,
            get_actionable_findings,
        )

        findings = raw.get("prior_actionable_findings")
        if findings is None and "previous_bot_reviews" in raw:
            findings = get_actionable_findings(raw["previous_bot_reviews"])

        has_prior_items = (
            bool(findings)
            or raw.get("prior_reviews_had_findings", False)
            or raw.get("prior_reviews_had_request_changes", False)
        )

        if not has_prior_items:
            parts.append(
                "\n### 📋 Pre-Fetched Prior Review Status\n"
                "Previous reviews on this PR identified 0 actionable critical issues, suggestions, or risks (clean pass or approved).\n\n"
                "NOTE ON RESOLUTION TRACKER: You MUST leave 'resolutions' as an empty list ([]) "
                "in SyncReviewResponse. Do NOT invent or backfill resolved items."
            )
        else:
            findings_text = format_findings_for_agent(findings or [])
            parts.append(
                f"\n### 📋 Pre-Fetched Prior Review Findings to Verify\n"
                f"{findings_text}\n\n"
                "### 🎯 ACTION DIRECTIVE FOR RESOLUTION TRACKER:\n"
                "Evaluate whether the new commits in this update address each of the prior items tracked above.\n"
                "For EACH tracked item:\n"
                "- If addressed/fixed by the new commits: include an entry in 'resolutions' with "
                "status='RESOLVED', category='CRITICAL'|'SUGGESTION'|'RISK', item_description='...', and concrete evidence from the new diff.\n"
                "- If not fixed: include an entry with status='UNRESOLVED' and evidence explaining why the issue persists."
            )

    if canonical in ("pull_request.opened", "pull_request.synchronize") or (
        canonical.startswith("issue_comment.") and "/review" in comment_body
    ):
        parts.append(
            "\n### 🚀 ACTION DIRECTIVE: FAST-PASS FORMAL AUDIT\n"
            "Evaluate the pre-fetched PR diff, AST verification findings, symbol impact analysis, "
            "and test coverage findings above.\n"
            "In Turn 1, output your completed CodeReviewResponse (or SyncReviewResponse) "
            "as a valid JSON object (verdict='APPROVE' or 'REQUEST_CHANGES').\n"
            "Deliver a thorough, staff-level architectural review: detailed multi-paragraph executive summary, "
            "subsystem-spanning verified invariants (at least 2-3 for APPROVE, each with 'invariant', 'path', 'line', and 'evidence'), and concrete maintainability suggestions/risks. "
            "Do NOT rubber-stamp with empty or 1-sentence sections. Output your formal review JSON immediately."
        )

    text = "\n".join(parts)
    text = _truncate_text_to_token_limit(
        text, max_tokens=get_max_input_tokens(), label="User payload"
    )
    return genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=text)],
    )


__all__ = [
    "AUDITOR_CONTEXT_INSTRUCTION",
    "CONVERSATIONAL_INSTRUCTION",
    "MAX_INPUT_TOKENS",
    "SYSTEM_INSTRUCTION",
    "_truncate_input_for_tier",
    "_truncate_text_to_token_limit",
    "build_user_message",
    "get_max_input_tokens",
]
