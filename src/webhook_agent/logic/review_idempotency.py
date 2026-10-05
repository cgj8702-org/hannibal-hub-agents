"""Backward-compatibility shim. Canonical module is webhook_agent.state.review_idempotency."""

from __future__ import annotations

import sys

from webhook_agent.state import review_idempotency as _canonical
from webhook_agent.state.review_idempotency import *  # noqa: F403

sys.modules[__name__] = _canonical
