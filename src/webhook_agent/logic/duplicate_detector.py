"""Backward-compatibility shim. Canonical module is webhook_agent.review.duplicate_detector."""

from __future__ import annotations

import sys

from webhook_agent.review import duplicate_detector as _canonical
from webhook_agent.review.duplicate_detector import *  # noqa: F403

sys.modules[__name__] = _canonical
