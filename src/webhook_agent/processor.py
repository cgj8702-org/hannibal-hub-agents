"""Webhook processor for the Hannibal Hub agents.

This module replaces the legacy worker logic with a more explicit, testable
router.  It is intentionally simple and heavily documented so that it can be
maintained by developers who are not familiar with the intricacies of the
GitHub webhook ecosystem.

Key responsibilities:
  * Normalise GitHub webhook events into a small set of canonical categories.
  * Decide whether an event should be processed based on its canonical
    value and a set of known noisy events.
  * Delegate to :class:`~agent_core.AgentCore` for the actual agent execution.

The implementation deliberately avoids importing heavy packages until the
``process_event`` method is called.
"""

from __future__ import annotations

import logging
from typing import Any
from github import Auth, Github

from .agent_core import AgentCore
from .bot_identity import _is_bot_event
from .github_credential_helper import (
    generate_jwt,
    get_installation_token,
    load_cached_token,
    load_private_key,
    save_cached_token,
)

logger = logging.getLogger("webhook_processor")


class WebhookProcessor:
    """Handles inbound webhook events.

    The processor expects a *normalized* payload – a dictionary that
    contains at least the following keys that match the GitHub webhook
    headers:
        * ``event_name`` – the X‑GitHub‑Event header value.
        * ``action`` – the action field nested inside the payload.
        * ``delivery_id`` – a unique id for the webhook delivery.
        * ``raw_payload`` – the original JSON body of the webhook.
    """

    def __init__(self) -> None:
        # Keep track of processed deliveries to prevent duplicate handling.
        self._processed_deliveries: set[str] = set()

        # Load essential GitHub credentials from the environment.
        try:
            self.app_id = int(os.environ["GITHUB_APP_ID"])
            self.installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
            self.private_key_path = os.environ["GITHUB_PRIVATE_KEY_PATH"]
        except KeyError as exc:
            logger.error("Missing required environment variable: %s", exc)
            raise