"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.symbol_graph."""

from __future__ import annotations

import sys

from webhook_agent.analysis import symbol_graph as _canonical
from webhook_agent.analysis.symbol_graph import *  # noqa: F403

sys.modules[__name__] = _canonical
