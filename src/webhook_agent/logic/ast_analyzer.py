"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.ast_analyzer."""

from __future__ import annotations

import sys

from webhook_agent.analysis import ast_analyzer as _canonical
from webhook_agent.analysis.ast_analyzer import *  # noqa: F403

sys.modules[__name__] = _canonical
