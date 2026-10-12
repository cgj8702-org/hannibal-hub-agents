"""Jules PR Adoption Proxy.

When Jules (google-labs-jules[bot]) authors a pull request, third-party review
tools like CodeRabbit AI refuse to review bot-authored PRs.

This module intercepts PR open events from Jules, closes the original Jules PR,
and reopens an identical PR on the exact same branch authenticated as the
machine user `cgj8702-agents`. This preserves the branch and Jules's commits while
allowing CodeRabbit AI and downstream automated tools to review normally.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

from github import Auth, Github

from webhook_agent.core.cancellation import pr_closed_registry
from webhook_agent.github.bot_identity import is_jules_sender

logger = logging.getLogger("webhook_agent.github.jules_proxy")


def get_agents_github_token() -> str | None:
    """Retrieve GitHub token for machine user cgj8702-agents.

    Checks environment variables first (populated from GCP Secret Manager in
    production), then falls back to local GitHub CLI credentials if available.
    """
    for env_var in ("CGJ8702_AGENTS_TOKEN", "AGENTS_GITHUB_TOKEN", "GH_TOKEN"):
        val = (os.environ.get(env_var) or "").strip()
        if val:
            return val

    # Fallback for local workstation / development environments
    gh_path = shutil.which("gh")
    if gh_path:
        try:
            res = subprocess.run(
                [gh_path, "auth", "token", "--user", "cgj8702-agents"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                token = res.stdout.strip()
                if token:
                    return token
        except Exception as exc:
            logger.debug("Failed to resolve cgj8702-agents token via gh CLI: %s", exc)

    return None


def get_agents_github_client() -> Github | None:
    """Return a PyGithub client authenticated as cgj8702-agents, or None if unconfigured."""
    token = get_agents_github_token()
    if not token:
        return None
    return Github(auth=Auth.Token(token))


def format_superseded_body(original_body: str, original_pr_number: int) -> str:
    """Format PR body with provenance note for the cgj8702-agents replacement PR."""
    body = original_body.rstrip()
    attribution = (
        f"\n\n---\n"
        f"*Reposted from #{original_pr_number} (authored by Jules; CodeRabbit skips bot-authored PRs). "
        f"Original branch preserved; this copy is opened under cgj8702-agents so automated review runs. "
        f"Closes #{original_pr_number} on merge.*"
    )
    return body + attribution


def reopen_jules_pr_as_agent(
    gh_app: Github,
    repo_name: str,
    payload: dict[str, Any],
    dry_run: bool = False,
) -> int | None:
    """Close original Jules PR and open an identical replacement PR as cgj8702-agents.

    Args:
        gh_app: PyGithub client authenticated as the GitHub App.
        repo_name: Full repository name (e.g. 'cgj8702-org/hannibal-hub').
        payload: Webhook delivery payload.
        dry_run: When True, simulates adoption without mutating GitHub state.

    Returns:
        The new PR number if successfully created, or None if skipped/failed.
    """
    raw = payload.get("raw_payload") or {}
    pr_data = raw.get("pull_request") or {}
    if not pr_data:
        return None

    pr_number = pr_data.get("number")
    if not pr_number:
        return None

    pr_state = (pr_data.get("state") or "").lower()
    if pr_state == "closed" or pr_data.get("merged") is True:
        logger.info(
            "Jules PR %s#%s is already closed/merged; skipping adoption.", repo_name, pr_number
        )
        return None

    head_info = pr_data.get("head") or {}
    base_info = pr_data.get("base") or {}
    head_branch = head_info.get("ref")
    base_branch = base_info.get("ref", "main")
    if not head_branch:
        logger.warning(
            "Jules PR %s#%s has no head branch ref; cannot reopen.", repo_name, pr_number
        )
        return None

    head_repo = head_info.get("repo") or {}
    base_repo = base_info.get("repo") or {}
    head_owner = (head_repo.get("owner") or {}).get("login")
    base_owner = (base_repo.get("owner") or {}).get("login")

    # If Jules worked across a fork, qualify the head ref
    if head_owner and base_owner and head_owner.lower() != base_owner.lower():
        head_ref_for_pr = f"{head_owner}:{head_branch}"
    else:
        head_ref_for_pr = head_branch

    title = pr_data.get("title") or f"PR from Jules #{pr_number}"
    original_body = pr_data.get("body") or ""
    draft = bool(pr_data.get("draft", False))
    new_body = format_superseded_body(original_body, int(pr_number))

    # Extract label names
    raw_labels = pr_data.get("labels") or []
    labels: list[str] = []
    for label_item in raw_labels:
        if isinstance(label_item, dict) and label_item.get("name"):
            labels.append(str(label_item["name"]))
        elif hasattr(label_item, "name"):
            labels.append(str(label_item.name))

    if dry_run:
        logger.info(
            "[dry-run] Would close Jules PR %s#%s and open replacement on branch '%s' under cgj8702-agents.",
            repo_name,
            pr_number,
            head_ref_for_pr,
        )
        return 999999

    agents_gh = get_agents_github_client()
    if agents_gh is None:
        logger.warning(
            "⚠️ Jules PR %s#%s detected, but cannot reopen as cgj8702-agents: "
            "no cgj8702-agents token configured in environment (CGJ8702_AGENTS_TOKEN).",
            repo_name,
            pr_number,
        )
        return None

    try:
        app_repo = gh_app.get_repo(repo_name)
        original_pr = app_repo.get_pull(int(pr_number))
    except Exception as exc:
        logger.warning("Could not fetch original Jules PR %s#%s: %s", repo_name, pr_number, exc)
        return None

    # Step 1: Close original Jules PR
    # GitHub does not allow two open PRs for the exact same head branch targeting the same base.
    # Closing the Jules PR allows the cgj8702-agents PR to immediately claim the head branch.
    try:
        original_pr.create_issue_comment(
            "Closing in favor of reopening an identical PR authored by @cgj8702-agents "
            "for CodeRabbit AI review compatibility."
        )
        original_pr.edit(state="closed")
        logger.info("Closed original Jules PR %s#%s.", repo_name, pr_number)
    except Exception as exc:
        logger.warning("Failed to close original Jules PR %s#%s: %s", repo_name, pr_number, exc)
        return None

    # Step 2: Open replacement PR as cgj8702-agents
    try:
        agent_repo = agents_gh.get_repo(repo_name)
        new_pr = agent_repo.create_pull(
            title=title,
            body=new_body,
            head=head_ref_for_pr,
            base=base_branch,
            draft=draft,
        )
        logger.info(
            "✅ Successfully opened replacement PR %s#%s authored by cgj8702-agents for branch '%s'.",
            repo_name,
            new_pr.number,
            head_ref_for_pr,
        )
    except Exception as exc:
        logger.exception(
            "💥 Failed to open replacement PR as cgj8702-agents for %s (branch '%s'): %s. Reopening original PR #%s.",
            repo_name,
            head_ref_for_pr,
            exc,
            pr_number,
        )
        # Rollback: restore original PR so it is not left stranded
        try:
            original_pr.edit(state="open")
            original_pr.create_issue_comment(
                "Reopened: failed to create replacement PR under @cgj8702-agents."
            )
            logger.info("Reopened original Jules PR %s#%s after rollback.", repo_name, pr_number)
        except Exception as rollback_exc:
            logger.exception(
                "Failed to reopen original PR %s#%s during rollback: %s",
                repo_name,
                pr_number,
                rollback_exc,
            )
        return None

    # Step 3: Copy labels to new PR
    if labels:
        try:
            new_pr.add_to_labels(*labels)
            logger.debug("Transferred labels %s to PR #%s", labels, new_pr.number)
        except Exception as label_exc:
            logger.warning("Could not transfer labels to PR #%s: %s", new_pr.number, label_exc)

    # Step 4: Register original PR as closed in registry to avoid race conditions
    pr_closed_registry.mark_closed(repo_name, int(pr_number))

    # Step 5: Post link comment on original PR pointing to the new PR
    try:
        original_pr.create_issue_comment(
            f"Superseded by #{new_pr.number} (authored by @cgj8702-agents)."
        )
    except Exception as comment_exc:
        logger.warning("Could not add link comment to original PR #%s: %s", pr_number, comment_exc)

    return int(new_pr.number)


def handle_jules_pr_opened(
    gh_app: Github,
    repo_name: str,
    payload: dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """Detect and process PR opened events authored by Jules.

    Returns:
        True if the PR was authored by Jules and successfully superseded under
        cgj8702-agents (signaling the caller to skip normal processing on the
        original PR), or False otherwise.
    """
    raw = payload.get("raw_payload") or {}
    action = raw.get("action") or payload.get("action")
    canonical = payload.get("canonical") or ""

    if action != "opened" and canonical != "pull_request.opened":
        return False

    pr_data = raw.get("pull_request") or {}
    pr_user = pr_data.get("user") or {}
    sender = payload.get("sender") or raw.get("sender") or {}

    if not (is_jules_sender(pr_user, raw) or is_jules_sender(sender, raw)):
        return False

    pr_number = pr_data.get("number")
    logger.info(
        "🤖 Detected PR %s#%s opened by Jules. Initiating cgj8702-agents adoption proxy...",
        repo_name,
        pr_number,
    )

    new_pr_number = reopen_jules_pr_as_agent(
        gh_app=gh_app,
        repo_name=repo_name,
        payload=payload,
        dry_run=dry_run,
    )

    if new_pr_number is not None:
        logger.info(
            "🚀 Jules PR %s#%s replaced by %s#%s under cgj8702-agents. Halting pipeline on original PR.",
            repo_name,
            pr_number,
            repo_name,
            new_pr_number,
        )
        return True

    return False
