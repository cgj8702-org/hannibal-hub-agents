"""Backward-compatibility shim. Canonical module is webhook_agent.models.model_chain."""

from __future__ import annotations

import sys

from webhook_agent.models import model_chain as _canonical
from webhook_agent.models.model_chain import *  # noqa: F403

sys.modules[__name__] = _canonical
