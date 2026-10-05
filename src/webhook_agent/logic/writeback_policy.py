"""Backward-compatibility shim. Canonical module is webhook_agent.review.writeback_policy."""

from __future__ import annotations

import sys

from webhook_agent.review import writeback_policy as _canonical
from webhook_agent.review.writeback_policy import *  # noqa: F403

sys.modules[__name__] = _canonical
