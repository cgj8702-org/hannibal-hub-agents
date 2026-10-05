"""Backward-compatibility shim. Canonical module is webhook_agent.models.rate_limiter."""

from __future__ import annotations

import sys

from webhook_agent.models import rate_limiter as _canonical
from webhook_agent.models.rate_limiter import *  # noqa: F403

sys.modules[__name__] = _canonical
