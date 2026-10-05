"""Backward-compatibility shim. Canonical module is webhook_agent.models.secret_manager."""

from __future__ import annotations

import sys

from webhook_agent.models import secret_manager as _canonical
from webhook_agent.models.secret_manager import *  # noqa: F403

sys.modules[__name__] = _canonical
