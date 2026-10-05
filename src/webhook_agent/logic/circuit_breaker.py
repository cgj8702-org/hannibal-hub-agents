"""Backward-compatibility shim. Canonical module is webhook_agent.state.circuit_breaker."""

from __future__ import annotations

import sys

from webhook_agent.state import circuit_breaker as _canonical
from webhook_agent.state.circuit_breaker import *  # noqa: F403

sys.modules[__name__] = _canonical
