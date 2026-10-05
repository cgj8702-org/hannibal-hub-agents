"""Backward-compatibility shim. Canonical module is webhook_agent.models.genai_provider."""

from __future__ import annotations

import sys

from webhook_agent.models import genai_provider as _canonical
from webhook_agent.models.genai_provider import *  # noqa: F403

sys.modules[__name__] = _canonical
