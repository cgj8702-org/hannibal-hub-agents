"""Backward-compatibility shim. Canonical module is webhook_agent.models.review_budget."""

from __future__ import annotations

import sys

from webhook_agent.models import review_budget as _canonical
from webhook_agent.models.review_budget import *  # noqa: F403

sys.modules[__name__] = _canonical
