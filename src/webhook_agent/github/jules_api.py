"""Jules REST API client for programmatic session management.

Provides low-level functions to interact with Jules sessions after they
have been created by Jules's GitHub App integration (triggered by the
``jules`` label attached by the human repository owner).

Endpoints used:
- GET    /v1alpha/sessions/{sessionId}
- GET    /v1alpha/sessions/{sessionId}/activities
- POST   /v1alpha/sessions/{sessionId}:approvePlan
- POST   /v1alpha/sessions/{sessionId}:sendMessage

Authentication: X-Goog-Api-Key header with JULES_API_KEY from Secret Manager.

This module does NOT create sessions — Jules creates them via its own GitHub
App integration when the owner attaches the ``jules`` label. This client only
discovers and manages sessions Jules already started.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("webhook_agent.github.jules_api")

JULES_BASE_URL = os.environ.get("JULES_BASE_URL", "https://jules.googleapis.com/v1alpha")

# Regex to extract a Jules session ID from a comment or URL.
# Matches URLs like https://jules.google.com/session/abc123
# or bare session IDs like sessions/abc123 or just abc123.
# Requires at least one digit so words like "Session" don't match.
_SESSION_ID_RE = re.compile(
    r"(?:https?://jules\.google\.com/session/|sessions/)?"
    r"(?=[a-zA-Z0-9_-]*\d)"
    r"([a-zA-Z0-9_-]{6,})"
)


def _api_key() -> str | None:
    """Resolve the Jules API key from environment."""
    return (os.environ.get("JULES_API_KEY") or "").strip() or None


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response | None:
    """Make an authenticated request to the Jules API."""
    key = _api_key()
    if not key:
        logger.warning("JULES_API_KEY not configured; skipping Jules API call %s %s", method, path)
        return None
    url = f"{JULES_BASE_URL}{path}"
    headers = {"x-goog-api-key": key}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp
    except httpx.HTTPError:
        logger.exception("Jules API request failed: %s %s", method, path)
        return None


def get_session(session_id: str) -> dict[str, Any] | None:
    """GET /v1alpha/sessions/{sessionId} — retrieve a single session.

    Returns the session JSON dict, or None on failure.
    """
    resp = _request("GET", f"/sessions/{session_id}")
    if resp is None:
        return None
    return resp.json()


def list_activities(session_id: str) -> list[dict[str, Any]]:
    """GET /v1alpha/sessions/{sessionId}/activities — list session activities.

    Returns a list of activity dicts, or an empty list on failure.
    """
    resp = _request("GET", f"/sessions/{session_id}/activities")
    if resp is None:
        return []
    data = resp.json()
    return data.get("activities", []) if isinstance(data, dict) else []


def approve_plan(session_id: str) -> bool:
    """POST /v1alpha/sessions/{sessionId}:approvePlan — approve a pending plan.

    Returns True on success, False on failure.
    """
    resp = _request("POST", f"/sessions/{session_id}:approvePlan", json={})
    return resp is not None


def send_message(session_id: str, prompt: str) -> bool:
    """POST /v1alpha/sessions/{sessionId}:sendMessage — send a message to Jules.

    Returns True on success, False on failure.
    """
    resp = _request("POST", f"/sessions/{session_id}:sendMessage", json={"prompt": prompt})
    return resp is not None


def session_state(session_id: str) -> str | None:
    """Return the session state string (e.g. ``AWAITING_PLAN_APPROVAL``) or None."""
    session = get_session(session_id)
    if session is None:
        return None
    return session.get("state")


def extract_session_id(text: str) -> str | None:
    """Extract a Jules session ID from arbitrary text (e.g. a comment body).

    Handles URLs like ``https://jules.google.com/session/abc123`` and bare IDs.
    """
    if not text:
        return None
    match = _SESSION_ID_RE.search(text)
    if match:
        return match.group(1)
    return None
