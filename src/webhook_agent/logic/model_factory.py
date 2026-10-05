"""Backward-compatibility shim. Canonical module is webhook_agent.models.model_factory."""

from __future__ import annotations

import sys

from webhook_agent.models import model_factory as _canonical
from webhook_agent.models.model_factory import *  # noqa: F403

sys.modules[__name__] = _canonical
