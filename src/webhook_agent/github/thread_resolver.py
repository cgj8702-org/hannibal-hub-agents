"""GitHub GraphQL client for inspecting and resolving PR review threads.

Enables automated and tool-driven resolution of inline review comment threads
(e.g., resolving addressed CodeRabbit or bot suggestions).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("webhook_agent.github.thread_resolver")

GITHUB_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

GET_PR_REVIEW_THREADS_QUERY = """
query GetPRReviewThreads($owner: String!, $name: String!, $prNumber: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 10) {
            nodes {
              id
              databaseId
              body
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
"""

RESOLVE_REVIEW_THREAD_MUTATION = """
mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread {
      id
      isResolved
    }
  }
}
"""

UNRESOLVE_REVIEW_THREAD_MUTATION = """
mutation UnresolveReviewThread($threadId: ID!) {
  unresolveReviewThread(input: { threadId: $threadId }) {
    thread {
      id
      isResolved
    }
  }
}
"""


@dataclass(frozen=True)
class ReviewComment:
    """Individual comment in a review thread."""

    id: str
    database_id: int | None
    body: str
    author_login: str


@dataclass(frozen=True)
class ReviewThread:
    """GitHub inline review comment thread."""

    id: str
    is_resolved: bool
    path: str
    line: int | None
    comments: list[ReviewComment] = field(default_factory=list)

    @property
    def first_comment(self) -> ReviewComment | None:
        return self.comments[0] if self.comments else None

    @property
    def authors(self) -> set[str]:
        return {c.author_login.lower() for c in self.comments if c.author_login}


def extract_token_from_gh(gh_or_token: Any) -> str | None:
    """Extract string authentication token from string, Github, or PyGithub model object."""
    if isinstance(gh_or_token, str) and gh_or_token.strip():
        return gh_or_token.strip()

    requester = getattr(gh_or_token, "requester", None) or getattr(gh_or_token, "_requester", None)
    if requester and hasattr(requester, "auth"):
        auth_obj = getattr(requester, "auth", None)
        token_val = getattr(auth_obj, "token", None)
        if token_val:
            return str(token_val).strip()

    auth_attr = getattr(gh_or_token, "_auth", None) or getattr(gh_or_token, "auth", None)
    if auth_attr and hasattr(auth_attr, "token"):
        return str(auth_attr.token).strip()

    return None


def fetch_pr_review_threads(
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    token: str,
    *,
    endpoint: str = GITHUB_GRAPHQL_ENDPOINT,
    timeout: float = 15.0,
) -> list[ReviewThread]:
    """Fetch all review comment threads for a pull request via GraphQL."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "hannibal-hub-agents",
    }
    payload = {
        "query": GET_PR_REVIEW_THREADS_QUERY,
        "variables": {
            "owner": repo_owner,
            "name": repo_name,
            "prNumber": pr_number,
        },
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning(
            "Failed fetching review threads for %s/%s#%d via GraphQL: %s",
            repo_owner,
            repo_name,
            pr_number,
            exc,
        )
        return []

    if "errors" in data:
        logger.warning(
            "GraphQL errors fetching review threads for %s/%s#%d: %s",
            repo_owner,
            repo_name,
            pr_number,
            data["errors"],
        )
        return []

    try:
        repo_data = data.get("data", {}).get("repository") or {}
        pr_data = repo_data.get("pullRequest") or {}
        thread_nodes = (pr_data.get("reviewThreads") or {}).get("nodes") or []

        threads: list[ReviewThread] = []
        for node in thread_nodes:
            thread_id = node.get("id", "")
            is_resolved = bool(node.get("isResolved", False))
            path = node.get("path", "")
            line = node.get("line")

            comments: list[ReviewComment] = []
            comment_nodes = (node.get("comments") or {}).get("nodes") or []
            for c_node in comment_nodes:
                author_login = ((c_node.get("author") or {}).get("login")) or ""
                comments.append(
                    ReviewComment(
                        id=c_node.get("id", ""),
                        database_id=c_node.get("databaseId"),
                        body=c_node.get("body", "") or "",
                        author_login=author_login,
                    )
                )

            threads.append(
                ReviewThread(
                    id=thread_id,
                    is_resolved=is_resolved,
                    path=path,
                    line=line if isinstance(line, int) else None,
                    comments=comments,
                )
            )
        return threads
    except Exception as parse_exc:
        logger.warning(
            "Could not parse review threads response for %s/%s#%d: %s",
            repo_owner,
            repo_name,
            pr_number,
            parse_exc,
        )
        return []


def resolve_review_thread(
    thread_id: str,
    token: str,
    *,
    endpoint: str = GITHUB_GRAPHQL_ENDPOINT,
    timeout: float = 15.0,
) -> bool:
    """Resolve a review comment thread by GraphQL threadId."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "hannibal-hub-agents",
    }
    payload = {
        "query": RESOLVE_REVIEW_THREAD_MUTATION,
        "variables": {
            "threadId": thread_id,
        },
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Failed to resolve review thread %s: %s", thread_id, exc)
        return False

    if "errors" in data:
        logger.warning("GraphQL errors resolving review thread %s: %s", thread_id, data["errors"])
        return False

    resolved = (
        data.get("data", {})
        .get("resolveReviewThread", {})
        .get("thread", {})
        .get("isResolved", False)
    )
    if resolved:
        logger.info("Successfully resolved review thread %s", thread_id)
    return bool(resolved)


def auto_resolve_coderabbit_threads(
    repo_full_name: str,
    pr_number: int,
    gh_or_token: Any,
    *,
    target_logins: Sequence[str] = ("coderabbitai", "coderabbitai[bot]"),
    endpoint: str = GITHUB_GRAPHQL_ENDPOINT,
) -> list[str]:
    """Find and resolve all open review threads started by target bots (e.g. CodeRabbit).

    Returns list of thread IDs that were successfully resolved.
    """
    token = extract_token_from_gh(gh_or_token)
    if not token:
        logger.warning("Cannot auto-resolve threads: no valid token could be extracted")
        return []

    parts = repo_full_name.split("/", 1)
    if len(parts) != 2:
        logger.warning("Invalid repo_full_name for thread resolution: %s", repo_full_name)
        return []
    owner, name = parts

    threads = fetch_pr_review_threads(owner, name, pr_number, token, endpoint=endpoint)
    normalized_targets = {t.lower() for t in target_logins}

    resolved_ids: list[str] = []
    for th in threads:
        if th.is_resolved:
            continue

        # Check if the thread initiator was one of our target bots
        first = th.first_comment
        first_author = first.author_login.lower() if first else ""
        if first_author in normalized_targets or any(a in normalized_targets for a in th.authors):
            logger.info(
                "Attempting to resolve %s thread %s on %s#%d (%s:%s)",
                first_author or "bot",
                th.id,
                repo_full_name,
                pr_number,
                th.path,
                th.line,
            )
            if resolve_review_thread(th.id, token, endpoint=endpoint):
                resolved_ids.append(th.id)

    return resolved_ids


def is_coderabbit_auto_resolve_enabled() -> bool:
    """Return True if automatic resolution of CodeRabbit threads on approval is enabled."""
    return os.environ.get("ENABLE_CODERABBIT_AUTO_RESOLVE", "true").lower() in (
        "1",
        "true",
        "yes",
    )
