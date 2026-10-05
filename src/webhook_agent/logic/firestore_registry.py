"""Backward-compatibility shim. Canonical module is webhook_agent.models.firestore_registry."""

from __future__ import annotations

import sys

from webhook_agent.models import firestore_registry as _canonical
from webhook_agent.models.firestore_registry import *  # noqa: F403

sys.modules[__name__] = _canonical
