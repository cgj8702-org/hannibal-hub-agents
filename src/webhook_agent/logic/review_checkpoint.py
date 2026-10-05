"""Backward-compatibility shim. Canonical module is webhook_agent.state.review_checkpoint."""

from __future__ import annotations

import sys

from webhook_agent.state import review_checkpoint as _canonical
from webhook_agent.state.review_checkpoint import *  # noqa: F403

sys.modules[__name__] = _canonical
